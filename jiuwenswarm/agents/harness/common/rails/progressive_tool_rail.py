# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Progressive tool rail - fixed tools schema with deferred tool access.

- eager_tools: always visible in the model tools schema
- deferred_tools: registered at runtime but hidden from schema; accessed via
  tools_search + invoke_tool

Fixed schema maximizes LLM prefix caching while keeping rarely used tools reachable.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager, nullcontext
from typing import Any, Callable, Iterable, Iterator

import anyio
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.tools.tools_search_tool import (
    ToolsSearchInput,
    ToolsSearchTool,
)
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import (
    InvokeToolInput,
    InvokeToolTool,
)
from jiuwenswarm.common.mcp_config import (
    OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG,
    OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX,
    bind_active_office_claw_mcp_tools,
    bind_office_claw_from_agent,
    get_active_office_claw_mcp_tool_ids,
    is_office_claw_tool_name_live_concurrent,
    resolve_active_office_claw_tool_id,
)


def _office_claw_tool_env_value(tool: Any, key: str) -> str:
    params = getattr(tool, "_params", None)
    if not isinstance(params, dict):
        return ""
    env = params.get("env")
    if not isinstance(env, dict):
        return ""
    return str(env.get(key) or "").strip()

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ProgressiveToolRail]"
_EAGER_DEEPRESEARCH_BINDINGS_KEY = "__eager_deepresearch_bindings__"
_DEEPRESEARCH_CONTEXT_TOOLS = frozenset({
    "deepresearch_execute",
    "deepresearch_stream",
    "deepresearch_prepare_rewrite",
    "deepresearch_commit_rewrite",
    "deepresearch_generate_rewrite_html",
})
_OFFICE_CLAW_SCHEDULE_THREAD_PIN_TOOLS = frozenset(
    {
        "office_claw_register_scheduled_task",
        "office_claw_preview_scheduled_task",
        "office_claw_update_scheduled_task",
    }
)


def _json_safe_value(value: Any) -> Any:
    """Convert tool invoke results (e.g. ToolOutput) to JSON-serializable values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(v) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe_value(model_dump(mode="json"))
        except TypeError:
            return _json_safe_value(model_dump())
    return str(value)


class ProgressiveToolRail(DeepAgentRail):
    """Progressive tool visibility with a fixed eager-tools schema.

    - eager_tools: always visible in the model tools schema
    - deferred_tools: registered in ability_manager but hidden from schema;
      accessed via tools_search (get schema) + invoke_tool (proxy call)
    - tools schema never changes, maximizing LLM prefix cache
    """

    priority = 80

    def __init__(
        self,
        *,
        enabled: bool = True,
        eager_tools: list[str] | None = None,
        language: str = "cn",
        agent_id: str | None = None,
        agent_card_id: str | None = None,
        enable_for_models: list[str] | None = None,
        deepresearch_context_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.enabled = bool(enabled)
        self.eager_tools = list(eager_tools or [])
        self.language = language or "cn"
        self.agent_id = agent_id
        self.agent_card_id = str(agent_card_id or "").strip() or None
        self._enable_for_models = [
            str(item).strip().lower()
            for item in (enable_for_models or [])
            if str(item).strip()
        ]
        self._cached_model_name = ""
        self._deepresearch_context_provider = deepresearch_context_provider

        if "tools_search" not in self.eager_tools:
            self.eager_tools.insert(0, "tools_search")
        if "invoke_tool" not in self.eager_tools:
            self.eager_tools.insert(1, "invoke_tool")

        self._deep_agent: Any = None
        self._runtime_agent: Any = None
        self._meta_tools: list[Any] | None = None
        self._meta_active = False
        self._owned_tool_ids: set[str] = set()
        self._cached_all_tool_infos: list[Any] = []
        self._cached_deferred_tool_infos: list[Any] = []
        # Request-scoped OfficeClaw MCP allowlist for this session agent.
        # Interaction rounds do not inherit the facade ContextVar, so
        # invoke_tool re-binds from this attribute before tool execution.
        self._office_claw_active_tool_ids: frozenset[str] | None = None
        self._office_claw_delivery_thread_id: str | None = None
        self._office_claw_invocation_id: str | None = None

    def set_office_claw_active_tool_ids(
        self,
        tool_ids: Iterable[str] | None,
        *,
        delivery_thread_id: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish or clear this rail's OfficeClaw request-scoped allowlist."""
        if tool_ids is None:
            self._office_claw_active_tool_ids = None
            self._office_claw_delivery_thread_id = None
            self._office_claw_invocation_id = None
            return
        self._office_claw_active_tool_ids = frozenset(
            str(tool_id).strip() for tool_id in tool_ids if str(tool_id).strip()
        )
        thread = str(delivery_thread_id or "").strip()
        inv = str(invocation_id or "").strip()
        self._office_claw_delivery_thread_id = thread or None
        self._office_claw_invocation_id = inv or None

    @contextmanager
    def _bind_deepresearch_context(self, tool_name: str) -> Iterator[None]:
        provider = self._deepresearch_context_provider
        if provider is None or tool_name not in _DEEPRESEARCH_CONTEXT_TOOLS:
            yield
            return

        from jiuwenswarm.agents.harness.common.tools.deepresearch import (
            push_deepresearch_route,
            reset_deepresearch_route,
        )
        from jiuwenswarm.common.local_env_config import (
            bind_agent_env_ns,
            bind_task_env_overlay,
            build_effective_env_overlay,
            reset_agent_env_ns,
            reset_task_env_overlay,
        )
        from jiuwenswarm.server.runtime.tenant_context import (
            bind_workspace_key,
            reset_workspace_key,
        )

        route = provider()
        service_id = str(route.get("service_id") or "default")
        agent_id = str(route.get("agent_id") or "default")
        workspace_key = str(route.get("workspace_key") or "default")
        with ExitStack() as cleanup:
            ns_token = bind_agent_env_ns(service_id, agent_id)
            cleanup.callback(reset_agent_env_ns, ns_token)
            wk_token = bind_workspace_key(workspace_key)
            cleanup.callback(reset_workspace_key, wk_token)
            overlay_token = bind_task_env_overlay(
                build_effective_env_overlay(
                    service_id=service_id,
                    agent_id=agent_id,
                )
            )
            cleanup.callback(reset_task_env_overlay, overlay_token)
            route_token = push_deepresearch_route(
                request_id=str(route.get("request_id") or ""),
                channel_id=str(route.get("channel_id") or ""),
                session_id=str(route.get("session_id") or ""),
                service_id=service_id,
                agent_id=agent_id,
                workspace_key=workspace_key,
                output_dir=str(route.get("output_dir") or ""),
            )
            cleanup.callback(reset_deepresearch_route, route_token)
            yield

    # ------------------------------------------------------------------
    # Model name resolution & enable check
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_model_name_from_ctx(ctx: AgentCallbackContext | None) -> str:
        """Read the effective model name for the current invoke."""
        if ctx is None:
            return ""
        agent = getattr(ctx, "agent", None)
        if agent is None:
            return ""

        direct_name = str(getattr(agent, "model_name", "") or "").strip()
        if direct_name:
            return direct_name

        config = getattr(agent, "_config", None)
        if config is not None:
            name = str(getattr(config, "model_name", "") or "").strip()
            if name:
                return name

        react_agent = (
            getattr(agent, "_react_agent", None)
            or getattr(agent, "react_agent", None)
        )
        if react_agent is not None:
            react_config = getattr(react_agent, "_config", None)
            if react_config is not None:
                name = str(getattr(react_config, "model_name", "") or "").strip()
                if name:
                    return name

        deep_config = getattr(agent, "deep_config", None)
        if deep_config is not None:
            model = getattr(deep_config, "model", None)
            model_config = (
                getattr(model, "model_config", None) if model is not None else None
            )
            name = str(getattr(model_config, "model_name", "") or "").strip()
            if name:
                return name
        return ""

    def _resolve_model_name_for_ctx(self, ctx: AgentCallbackContext | None) -> str:
        """Resolve model name from ctx, falling back to cached name."""
        name = self._resolve_model_name_from_ctx(ctx)
        if name:
            self._cached_model_name = name
            return name
        return self._cached_model_name

    def _is_lazy_load_enabled_for_model(self, model_name: str) -> bool:
        """Empty whitelist = all models; otherwise substring match."""
        if not self._enable_for_models:
            return True
        if not model_name:
            return False
        lowered = model_name.lower()
        return any(pattern in lowered for pattern in self._enable_for_models)

    def _lazy_load_active_for_ctx(self, ctx: AgentCallbackContext | None) -> bool:
        if not self.enabled:
            return False
        model_name = self._resolve_model_name_for_ctx(ctx)
        if not self._is_lazy_load_enabled_for_model(model_name):
            logger.info(
                "%s lazy load bypassed for model=%s enable_for_models=%s",
                _LOG_PREFIX,
                model_name,
                self._enable_for_models,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def update_agent_card_id(self, card_id: str | None) -> None:
        """Update session card id and invalidate cached meta tools."""
        resolved = str(card_id or "").strip() or None
        if resolved and resolved != self.agent_card_id:
            self.agent_card_id = resolved
            self._meta_tools = None
            self._meta_active = False

    def init(self, agent: Any) -> None:
        # ``AgentCard.id`` is a persistence identity shared by every session
        # adapter for the same agent. Constructor already receives a
        # session-scoped owner id (``{card}_s_{session}``). Overwriting it with
        # the shared card id collapses ``invoke_tool`` / ``tools_search`` onto
        # one process-global resource_mgr entry — last session's rail (and its
        # OfficeClaw pin) then serves every concurrent session.
        if not self.agent_card_id:
            card = getattr(agent, "card", None)
            resolved_card_id = str(getattr(card, "id", "") or "").strip() or None
            self.update_agent_card_id(resolved_card_id)
        self._deep_agent = agent
        self._runtime_agent = agent
        self.invalidate_deferred_tool_cache()

    def uninit(self, agent: Any) -> None:
        self._set_meta_tools_active(False)
        self._deep_agent = None
        self._runtime_agent = None
        self._meta_tools = None

    def invalidate_deferred_tool_cache(self) -> None:
        """Clear deferred tool caches (after agent rebind or reload)."""
        self._cached_all_tool_infos = []
        self._cached_deferred_tool_infos = []

    def _resolve_runtime_agent(
        self,
        ctx: AgentCallbackContext | None = None,
    ) -> Any:
        """Resolve the agent whose ability_manager is authoritative."""
        agent = None
        if ctx is not None:
            agent = getattr(ctx, "agent", None)
        if agent is None:
            agent = self._runtime_agent
        if agent is None:
            agent = self._deep_agent
        if agent is not None and agent is not self._deep_agent:
            logger.debug(
                "%s runtime agent swap old_id=%s new_id=%s",
                _LOG_PREFIX,
                id(self._deep_agent),
                id(agent),
            )
            self._deep_agent = agent
            self.invalidate_deferred_tool_cache()
        return agent

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Bind trusted tenant context for the direct DeepResearch entry."""
        inputs = getattr(ctx, "inputs", None)
        tool_call = getattr(inputs, "tool_call", None)
        tool_name = str(
            getattr(inputs, "tool_name", "")
            or getattr(tool_call, "name", "")
            or ""
        ).strip()
        if tool_name != "deepresearch_execute":
            return
        manager = self._bind_deepresearch_context(tool_name)
        manager.__enter__()
        tool_call_id = str(getattr(tool_call, "id", "") or id(inputs))
        bindings = ctx.extra.setdefault(_EAGER_DEEPRESEARCH_BINDINGS_KEY, {})
        bindings[tool_call_id] = manager

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Release a direct DeepResearch entry's tenant bindings."""
        inputs = getattr(ctx, "inputs", None)
        tool_call = getattr(inputs, "tool_call", None)
        tool_name = str(
            getattr(inputs, "tool_name", "")
            or getattr(tool_call, "name", "")
            or ""
        ).strip()
        if tool_name != "deepresearch_execute":
            return
        tool_call_id = str(getattr(tool_call, "id", "") or id(inputs))
        bindings = ctx.extra.get(_EAGER_DEEPRESEARCH_BINDINGS_KEY, {})
        manager = bindings.pop(tool_call_id, None) if isinstance(bindings, dict) else None
        if not bindings:
            ctx.extra.pop(_EAGER_DEEPRESEARCH_BINDINGS_KEY, None)
        if manager is not None:
            manager.__exit__(None, None, None)

    # ------------------------------------------------------------------
    # Meta tool management
    # ------------------------------------------------------------------

    def _meta_tool_scope_id(self) -> str | None:
        """Session/subagent scope for meta tool resource ids."""
        return self.agent_card_id or (
            str(self.agent_id or "").strip() or None
        )

    @staticmethod
    def _legacy_meta_tool_ids(
        tool: Any, tenant_agent_id: str | None
    ) -> set[str]:
        """Unqualified or tenant-scoped ids that must not shadow session tools."""
        base = str(getattr(type(tool), "TOOL_ID", "") or "").strip()
        if not base:
            return set()
        legacy = {base}
        if tenant_agent_id:
            legacy.add(f"{base}_{tenant_agent_id}")
        return legacy

    def _meta_tool_instances(self) -> list[Any]:
        if self._meta_tools is None:
            scope_id = self._meta_tool_scope_id()
            self._meta_tools = [
                ToolsSearchTool(
                    self._search_tools,
                    language=self.language,
                    agent_card_id=scope_id,
                ),
                InvokeToolTool(
                    self._invoke_target_tool,
                    language=self.language,
                    agent_card_id=scope_id,
                ),
            ]
        return self._meta_tools

    def _register_meta_tool_in_resource_mgr(self, tool: Any) -> None:
        """Register or replace session-scoped meta tool in resource_mgr."""
        qualified_id = str(tool.card.id)
        for stale_id in self._legacy_meta_tool_ids(tool, self.agent_id):
            if stale_id != qualified_id:
                try:
                    Runner.resource_mgr.remove_tool(stale_id)
                except Exception as exc:
                    logger.debug(
                        "%s failed to remove stale meta tool %s: %s",
                        _LOG_PREFIX,
                        stale_id,
                        exc,
                    )
        try:
            Runner.resource_mgr.remove_tool(qualified_id)
        except Exception as exc:
            logger.debug(
                "%s failed to remove meta tool %s before re-add: %s",
                _LOG_PREFIX,
                qualified_id,
                exc,
            )
        Runner.resource_mgr.add_tool(tool)
        self._owned_tool_ids.add(qualified_id)

    def _set_meta_tools_active(self, active: bool) -> None:
        if active == self._meta_active or self._deep_agent is None:
            return

        self._meta_active = active
        ability_manager = getattr(self._deep_agent, "ability_manager", None)

        for tool in self._meta_tool_instances():
            if active:
                try:
                    self._register_meta_tool_in_resource_mgr(tool)
                except Exception as exc:
                    logger.warning(
                        "%s failed to add meta tool resource %s: %s",
                        _LOG_PREFIX,
                        tool.card.id,
                        exc,
                    )
                if ability_manager is not None:
                    try:
                        existing = ability_manager.get(tool.card.name)
                        if existing is not None:
                            ability_manager.remove(tool.card.name)
                        ability_manager.add(tool.card)
                    except Exception as exc:
                        logger.warning(
                            "%s failed to add meta tool card %s: %s",
                            _LOG_PREFIX,
                            tool.card.name,
                            exc,
                        )
                continue

            if ability_manager is not None:
                try:
                    ability_manager.remove(tool.card.name)
                except Exception as exc:
                    logger.warning(
                        "%s failed to remove meta tool card %s: %s",
                        _LOG_PREFIX,
                        tool.card.name,
                        exc,
                    )
            if tool.card.id not in self._owned_tool_ids:
                continue
            try:
                Runner.resource_mgr.remove_tool(tool.card.id)
            except Exception as exc:
                logger.warning(
                    "%s failed to remove meta tool resource %s: %s",
                    _LOG_PREFIX,
                    tool.card.id,
                    exc,
                )
            self._owned_tool_ids.discard(tool.card.id)

        if not active:
            self.invalidate_deferred_tool_cache()

    # ------------------------------------------------------------------
    # Rail hooks
    # ------------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """Register meta tools when active; cache deferred tools for navigation."""
        if getattr(ctx, "agent", None) is not None:
            self._runtime_agent = ctx.agent
        self._resolve_runtime_agent(ctx)
        active = self._lazy_load_active_for_ctx(ctx)
        self._set_meta_tools_active(active)
        if not active:
            return

        await self._refresh_deferred_tool_cache()

        logger.info(
            "%s invoke total=%s eager=%s deferred=%s",
            _LOG_PREFIX,
            len(self._cached_all_tool_infos),
            len(self.eager_tools),
            len(self._cached_deferred_tool_infos),
        )

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Filter tools to only include eager_tools."""
        if not self._lazy_load_active_for_ctx(ctx):
            return

        if getattr(ctx, "agent", None) is not None:
            self._runtime_agent = ctx.agent
        self._resolve_runtime_agent(ctx)
        await self._refresh_deferred_tool_cache_if_stale()

        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return

        tools = getattr(inputs, "tools", None)
        if not isinstance(tools, list):
            return

        original_count = len(tools)
        filtered_tools = [
            tool for tool in tools
            if str(getattr(tool, "name", "") or "") in self.eager_tools
        ]
        inputs.tools = filtered_tools

        removed = sorted(set(
            str(getattr(tool, "name", "") or "")
            for tool in tools
        ) - set(
            str(getattr(tool, "name", "") or "")
            for tool in filtered_tools
        ))

        logger.info(
            "%s filter tools %s -> %s removed=%s",
            _LOG_PREFIX,
            original_count,
            len(filtered_tools),
            removed,
        )

        await self._add_navigation_section(ctx)

    # ------------------------------------------------------------------
    # Navigation prompt
    # ------------------------------------------------------------------

    async def _add_navigation_section(self, ctx: AgentCallbackContext) -> None:
        """Add tool navigation prompt section."""
        if ctx.agent is None:
            return

        navigation_section = await self._build_navigation_section()
        if navigation_section is None:
            return

        spb = getattr(ctx.agent, "system_prompt_builder", None)
        if spb is not None and hasattr(spb, "add_section"):
            spb.add_section(navigation_section)

    async def _build_navigation_section(self) -> PromptSection | None:
        """Build navigation prompt section for deferred tools."""
        if not self._cached_deferred_tool_infos:
            return None

        entries_cn = await self._build_navigation_entries(language="cn")
        entries_en = await self._build_navigation_entries(language="en")

        return PromptSection(
            name=SectionName.TOOL_NAVIGATION,
            content={
                "cn": self._build_navigation_prompt(entries_cn, language="cn"),
                "en": self._build_navigation_prompt(entries_en, language="en"),
            },
            priority=70,
        )

    async def _build_navigation_entries(
        self,
        language: str = "cn",
    ) -> list[str]:
        """Build navigation entries for deferred tools."""
        sorted_tools = sorted(
            self._cached_deferred_tool_infos,
            key=lambda t: str(getattr(t, "name", "") or ""),
        )

        entries: list[str] = []
        punctuation = "：" if language == "cn" else ":"

        for tool in sorted_tools:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            description = str(getattr(tool, "description", "") or "")
            brief_desc = description[:160] if len(description) > 160 else description
            entries.append(f"- {name}{punctuation} {brief_desc}")

        return entries

    @staticmethod
    def _build_navigation_prompt(
        entries: list[str],
        *,
        language: str,
    ) -> str:
        """Build navigation prompt text."""
        items = [item for item in entries if item]

        if language == "en":
            header = (
                "## Deferred Tool Navigation (Not in tools list)\n\n"
                "**IMPORTANT: The tools listed below are NOT in your tools list. "
                "You CANNOT call them directly.**\n\n"
                "To use any tool from this list:\n"
                "1. **First** call `tools_search` with `tool_name` (exact registered name) "
                "to get its complete schema.\n"
                "2. **Then** call `invoke_tool` with exact `tool_name` and `arguments`.\n"
                "3. **Do NOT call `tools_search` again** for a tool whose schema already "
                "appears in conversation history — reuse it and call `invoke_tool` directly.\n"
                "4. **One tool per question.** If you already have (or have previously used) a tool "
                "that can answer the user's question, do NOT call `tools_search` to discover "
                "alternative tools or invoke multiple tools to cross-validate — one tool is "
                "sufficient unless the user explicitly requests comparison.\n\n"
                "**Do NOT attempt to call these tools directly - they will fail.**\n\n"
            )
            empty = "- (no deferred tools available)"
        else:
            header = (
                "## 按需可见工具导航（不可直接调用）\n\n"
                "**重要提示：以下工具不在当前 tools 列表中，无法直接调用。**\n\n"
                "使用方法：\n"
                "1. **必须先**调用 `tools_search`，传入与导航列表一致的 `tool_name`，获取完整参数 schema。\n"
                "2. **然后**调用 `invoke_tool`，传入精确 `tool_name` 和根据 schema 构造的 `arguments`。\n"
                "3. **如果当前对话历史中已有该工具的 schema（来自之前的 `tools_search` 返回结果），"
                "切勿再次调用 `tools_search`，直接根据已有 schema 调用 `invoke_tool` 即可。\n"
                "4. **一个问题只用一个工具。** 如果你已经有（或之前已用过）能回答用户问题的工具，"
                "切勿再调 `tools_search` 去发现替代工具，也不要调用多个工具做交叉验证——"
                "一个工具就够了，除非用户明确要求比较。\n\n"
                "**切勿直接调用以下工具——直接调用会失败。**\n\n"
            )
            empty = "- （当前无按需可见工具）"

        return header + ("\n".join(items) if items else empty)

    # ------------------------------------------------------------------
    # Deferred tool cache
    # ------------------------------------------------------------------

    async def _get_all_tool_infos(self, agent: Any = None) -> list[Any]:
        """Get all registered tool infos from ability_manager."""
        resolved = agent or self._runtime_agent or self._deep_agent
        if resolved is None:
            return []

        ability_manager = getattr(resolved, "ability_manager", None)
        if ability_manager is None:
            return []

        try:
            return list(ability_manager.list())
        except Exception as exc:
            logger.warning(
                "%s failed to list ability_manager tools: error=%s",
                _LOG_PREFIX,
                exc,
            )
            return []

    async def _refresh_deferred_tool_cache(
        self, agent: Any = None
    ) -> None:
        """Refresh cached tool lists from live ability_manager."""
        resolved = agent or self._resolve_runtime_agent()
        all_tools = await self._get_all_tool_infos(resolved)
        self._cached_all_tool_infos = all_tools
        self._cached_deferred_tool_infos = [
            tool
            for tool in all_tools
            if str(getattr(tool, "name", "") or "") not in self.eager_tools
        ]

    @staticmethod
    def _tool_name_set(tools: list[Any]) -> set[str]:
        """Return registered tool names from ability_manager entries."""
        return {
            str(getattr(tool, "name", "") or "")
            for tool in tools
            if str(getattr(tool, "name", "") or "")
        }

    @staticmethod
    def _tool_id_set(tools: list[Any]) -> set[str]:
        """Return registered tool ids from ability_manager entries.

        Same-named cards can swap ids when a request-scoped MCP rebinds
        (e.g. OfficeClaw MCP re-registers per request with a fresh hash).
        Comparing ids in addition to names lets the staleness check catch
        such rebinds that name-only comparison misses.
        """
        return {
            str(getattr(tool, "id", "") or "")
            for tool in tools
            if str(getattr(tool, "id", "") or "")
        }

    async def _refresh_deferred_tool_cache_if_stale(self) -> None:
        """Refresh cache when ability_manager has tools not in cache."""
        agent = self._resolve_runtime_agent()
        live_tools = await self._get_all_tool_infos(agent)
        if len(live_tools) != len(self._cached_all_tool_infos):
            await self._refresh_deferred_tool_cache(agent)
            return
        if self._tool_name_set(live_tools) != self._tool_name_set(
            self._cached_all_tool_infos
        ):
            await self._refresh_deferred_tool_cache(agent)
            return
        if self._tool_id_set(live_tools) != self._tool_id_set(
            self._cached_all_tool_infos
        ):
            await self._refresh_deferred_tool_cache(agent)
            return
        live_deferred = [
            tool
            for tool in live_tools
            if str(getattr(tool, "name", "") or "") not in self.eager_tools
        ]
        if live_deferred and not self._cached_deferred_tool_infos:
            await self._refresh_deferred_tool_cache(agent)

    # ------------------------------------------------------------------
    # Meta tool implementations
    # ------------------------------------------------------------------

    def _find_deferred_tool_matches(
        self, tool_name_key: str
    ) -> list[Any]:
        """Return deferred tools whose name matches (case-insensitive)."""
        matches = []
        for tool in self._cached_deferred_tool_infos:
            name = str(getattr(tool, "name", "") or "")
            if name.lower() == tool_name_key:
                matches.append(tool)
        return matches

    async def _search_tools(
        self,
        session: Any,
        params: ToolsSearchInput,
    ) -> dict[str, Any]:
        """Look up a deferred tool by registered name and return its schema."""
        tool_name = str(params.tool_name or "").strip()
        tool_name_key = tool_name.lower()

        if not tool_name:
            return {
                "success": False,
                "matches": [],
                "message": "tool_name is required",
            }

        matches = self._find_deferred_tool_matches(tool_name_key)
        if not matches:
            await self._refresh_deferred_tool_cache()
            matches = self._find_deferred_tool_matches(tool_name_key)
        if not matches:
            runtime = self._runtime_agent
            if runtime is not None and runtime is not self._deep_agent:
                logger.info(
                    "%s search retry with runtime agent old_id=%s new_id=%s",
                    _LOG_PREFIX,
                    id(self._deep_agent),
                    id(runtime),
                )
                self._deep_agent = runtime
                self.invalidate_deferred_tool_cache()
                await self._refresh_deferred_tool_cache(runtime)
                matches = self._find_deferred_tool_matches(tool_name_key)

        result_matches = []
        for tool in matches:
            name = str(getattr(tool, "name", "") or "")
            description = str(getattr(tool, "description", "") or "")
            input_params = getattr(tool, "input_params", {}) or {}
            tool_id = str(getattr(tool, "id", "") or "")
            if not self._tool_instance_available(name, tool_id):
                logger.warning(
                    "%s search skip dead instance: tool_name=%s tool_id=%s",
                    _LOG_PREFIX,
                    name,
                    tool_id,
                )
                continue

            result_matches.append({
                "name": name,
                "description": description,
                "input_schema": input_params,
            })

        logger.info(
            "%s search tool_name=%s matches=%s",
            _LOG_PREFIX,
            tool_name,
            len(result_matches),
        )

        if matches and not result_matches:
            return {
                "success": False,
                "matches": [],
                "count": 0,
                "message": (
                    f"工具 '{tool_name}' 的 schema 可见但实例不可用"
                    "（可能被并发请求清理），请稍后重试。"
                ),
            }

        return {
            "success": bool(result_matches),
            "matches": result_matches,
            "count": len(result_matches),
            "message": (
                f"已找到工具 '{tool_name}'，请根据 input_schema 构造 arguments 后调用 invoke_tool。"
                if result_matches
                else f"未找到名为 '{tool_name}' 的按需可见工具，请检查名称是否与导航列表一致。"
            ),
        }

    @staticmethod
    def _lookup_tool_instance(tool_id: str) -> Any | None:
        """Best-effort resource_mgr lookup; never raises to callers."""
        normalized = str(tool_id or "").strip()
        if not normalized:
            return None
        try:
            return Runner.resource_mgr.get_tool(normalized)
        except Exception as exc:
            logger.warning(
                "%s failed to get tool instance: tool_id=%s error=%s",
                _LOG_PREFIX,
                normalized,
                exc,
            )
            return None

    def _owned_office_claw_tool_id(self, tool_name: str) -> str:
        """Return this rail's allowlisted request-scoped id for ``tool_name``."""
        owned = getattr(self, "_office_claw_active_tool_ids", None)
        if not owned:
            return ""
        suffix = f".{tool_name}"
        matches = [
            tool_id
            for tool_id in owned
            if tool_id.endswith(suffix) or tool_id.rsplit(".", 1)[-1] == tool_name
        ]
        if not matches:
            return ""
        preferred = []
        for tool_id in matches:
            if tool_id.startswith(OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX) and tool_id.endswith(
                suffix
            ):
                preferred.append(tool_id)
        return preferred[0] if preferred else matches[0]

    def _is_foreign_office_claw_tool_id(
        self,
        tool_id: str,
        tool_name: str,
        *,
        allowed: frozenset[str] | None,
    ) -> bool:
        """True when ``tool_id`` must not be used for this request."""
        if not tool_id.startswith(OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX):
            return False
        owned = getattr(self, "_office_claw_active_tool_ids", None)
        if owned is not None:
            return tool_id not in owned
        if allowed is not None:
            return tool_id not in allowed
        # No request binding: never trust a live request-scoped card under
        # concurrency (ask_user resume races), and never trust one without a
        # rail pin even when only one registration remains — it may belong to
        # another session after our own MCP was cleaned.
        if is_office_claw_tool_name_live_concurrent(tool_name):
            return True
        return not bool(
            str(getattr(self, "_office_claw_invocation_id", None) or "").strip()
        )

    def _tool_instance_available(self, tool_name: str, tool_id: str) -> bool:
        """True when cache id or this request's active OfficeClaw id still resolves."""
        owned_id = self._owned_office_claw_tool_id(tool_name)
        if owned_id:
            return self._lookup_tool_instance(owned_id) is not None
        if tool_id.startswith(OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX):
            if self._is_foreign_office_claw_tool_id(
                tool_id, tool_name, allowed=get_active_office_claw_mcp_tool_ids()
            ):
                return False
        if self._lookup_tool_instance(tool_id) is not None:
            return True
        active_id = resolve_active_office_claw_tool_id(tool_name)
        if not active_id:
            return False
        if active_id == tool_id:
            return False
        return self._lookup_tool_instance(active_id) is not None

    async def _resolve_tool_instance_with_recovery(
        self,
        tool_name: str,
        preferred_id: str,
    ) -> tuple[Any | None, str, str]:
        """Resolve a live tool instance, recovering from stale card ids.

        Order:
        1. this rail's request-scoped OfficeClaw allowlist (session pin)
        2. this request's ``bind_active_office_claw_mcp_tools`` allowlist
        3. preferred_id from deferred cache / AbilityManager card — skipped when
           it is a foreign ``office-claw-request-*`` id
        4. refresh AbilityManager cache and retry if the live card id changed

        Returns ``(tool, resolved_id, refreshed_id_for_diagnostics)``.
        """
        owned_id = self._owned_office_claw_tool_id(tool_name)
        if owned_id:
            target_tool = self._lookup_tool_instance(owned_id)
            if target_tool is not None:
                if owned_id != preferred_id:
                    logger.info(
                        "%s rail-owned OfficeClaw tool_id preferred: tool_name=%s "
                        "preferred_id=%s owned_id=%s",
                        _LOG_PREFIX,
                        tool_name,
                        preferred_id,
                        owned_id,
                    )
                return target_tool, owned_id, ""
            logger.warning(
                "%s rail-owned OfficeClaw tool instance missing: tool_name=%s "
                "owned_id=%s preferred_id=%s",
                _LOG_PREFIX,
                tool_name,
                owned_id,
                preferred_id,
            )
            # Fail closed: never fall through to another request's live tool.
            return None, owned_id, ""

        active_id = resolve_active_office_claw_tool_id(tool_name)
        if active_id:
            target_tool = self._lookup_tool_instance(active_id)
            if target_tool is not None:
                if active_id != preferred_id:
                    logger.info(
                        "%s active OfficeClaw tool_id preferred: tool_name=%s "
                        "preferred_id=%s active_id=%s",
                        _LOG_PREFIX,
                        tool_name,
                        preferred_id,
                        active_id,
                    )
                return target_tool, active_id, ""

        allowed = get_active_office_claw_mcp_tool_ids()
        preferred_is_foreign_office_claw = self._is_foreign_office_claw_tool_id(
            preferred_id, tool_name, allowed=allowed
        )
        if not preferred_is_foreign_office_claw:
            target_tool = self._lookup_tool_instance(preferred_id)
            if target_tool is not None:
                return target_tool, preferred_id, ""
        elif preferred_id:
            logger.info(
                "%s skip foreign OfficeClaw preferred_id: tool_name=%s preferred_id=%s "
                "allowed_bound=%s concurrent=%s rail_bound=%s",
                _LOG_PREFIX,
                tool_name,
                preferred_id,
                allowed is not None,
                is_office_claw_tool_name_live_concurrent(tool_name),
                getattr(self, "_office_claw_active_tool_ids", None) is not None,
            )

        logger.info(
            "%s stale tool_id retry: tool_name=%s old_id=%s",
            _LOG_PREFIX,
            tool_name,
            preferred_id,
        )
        await self._refresh_deferred_tool_cache()
        refreshed_card = None
        for tool in self._cached_deferred_tool_infos:
            if str(getattr(tool, "name", "") or "") == tool_name:
                refreshed_card = tool
                break
        refreshed_id = (
            str(getattr(refreshed_card, "id", "") or "") if refreshed_card is not None else ""
        )
        if refreshed_id and refreshed_id != preferred_id:
            refreshed_is_foreign = self._is_foreign_office_claw_tool_id(
                refreshed_id, tool_name, allowed=allowed
            )
            if not refreshed_is_foreign:
                target_tool = self._lookup_tool_instance(refreshed_id)
                if target_tool is not None:
                    return target_tool, refreshed_id, refreshed_id

        if active_id:
            target_tool = self._lookup_tool_instance(active_id)
            if target_tool is not None:
                logger.info(
                    "%s active OfficeClaw tool_id fallback: tool_name=%s active_id=%s",
                    _LOG_PREFIX,
                    tool_name,
                    active_id,
                )
                return target_tool, active_id, refreshed_id or active_id

        return None, preferred_id, refreshed_id

    async def _invoke_target_tool(
        self,
        session: Any,
        params: InvokeToolInput,
        **kwargs,
    ) -> dict[str, Any]:
        """Invoke a deferred tool with validated arguments."""
        owned = self._office_claw_active_tool_ids
        runtime_agent = self._resolve_runtime_agent()
        # AM carrier may be stale/cleared across ask_user resume races; bind it
        # first, then re-assert the rail pin so this session's request wins.
        with bind_office_claw_from_agent(runtime_agent):
            bind_cm = (
                bind_active_office_claw_mcp_tools(owned)
                if owned is not None
                else nullcontext()
            )
            with bind_cm:
                return await self._invoke_target_tool_with_binding(
                    session, params, **kwargs
                )

    async def _invoke_target_tool_with_binding(
        self,
        session: Any,
        params: InvokeToolInput,
        **kwargs,
    ) -> dict[str, Any]:
        """Invoke a deferred tool after OfficeClaw allowlist ContextVar is bound."""
        tool_name = str(params.tool_name or "").strip()
        arguments = dict(params.arguments or {})

        if not tool_name:
            return {
                "success": False,
                "error": "tool_name is required",
                "tool_name": "",
            }

        if tool_name in self.eager_tools:
            return {
                "success": False,
                "error": f"工具 '{tool_name}' 是常驻可见工具，可直接调用，无需通过 invoke_tool。",
                "tool_name": tool_name,
            }

        target_tool_card = None
        for tool in self._cached_deferred_tool_infos:
            if str(getattr(tool, "name", "") or "") == tool_name:
                target_tool_card = tool
                break

        if target_tool_card is None:
            agent = self._resolve_runtime_agent()
            ability_manager = getattr(agent, "ability_manager", None)
            lookup_timeout = getattr(AbilityManager, "_resolve_call_timeout")(None)
            try:
                with anyio.fail_after(lookup_timeout):
                    if ability_manager is not None:
                        try:
                            await ability_manager.list_tool_info()
                        except Exception as exc:
                            logger.warning(
                                "%s invoke fallback list_tool_info failed: %s",
                                _LOG_PREFIX,
                                exc,
                            )
                    await self._refresh_deferred_tool_cache()
                    for tool in self._cached_deferred_tool_infos:
                        if str(getattr(tool, "name", "") or "") == tool_name:
                            target_tool_card = tool
                            break
            except TimeoutError:
                return {
                    "success": False,
                    "error": (
                        f"Tool lookup for '{tool_name}' timed out after "
                        f"{lookup_timeout}s"
                    ),
                    "tool_name": tool_name,
                }

        owned_id = self._owned_office_claw_tool_id(tool_name)
        if target_tool_card is None and not owned_id:
            concurrent = is_office_claw_tool_name_live_concurrent(tool_name)
            if concurrent or tool_name.startswith("office_claw_"):
                return {
                    "success": False,
                    "error": (
                        f"工具 '{tool_name}' 当前不可用（可能被并发会话清理），"
                        "请稍后重试注册/调用。"
                    ),
                    "tool_name": tool_name,
                }
            return {
                "success": False,
                "error": f"工具 '{tool_name}' 未注册或不在按需可见工具列表中。",
                "tool_name": tool_name,
            }

        preferred_id = owned_id or str(getattr(target_tool_card, "id", "") or "")
        target_tool, target_tool_id, refreshed_id = await self._resolve_tool_instance_with_recovery(
            tool_name,
            preferred_id,
        )

        if target_tool is None:
            logger.warning(
                "%s invoke tool=%s failed: instance not found, "
                "tool_id=%s, refresh_attempted=True, refreshed_id=%s, active_id=%s "
                "owned_id=%s concurrent=%s",
                _LOG_PREFIX,
                tool_name,
                target_tool_id,
                refreshed_id,
                resolve_active_office_claw_tool_id(tool_name) or "",
                owned_id or "",
                is_office_claw_tool_name_live_concurrent(tool_name),
            )
            if tool_name.startswith("office_claw_") or is_office_claw_tool_name_live_concurrent(
                tool_name
            ):
                error = (
                    f"无法获取工具 '{tool_name}' 的实例"
                    "（可能被并发请求清理），请稍后重试。"
                )
            else:
                error = f"无法获取工具 '{tool_name}' 的实例。"
            return {
                "success": False,
                "error": error,
                "tool_name": tool_name,
            }

        expected_invocation = str(self._office_claw_invocation_id or "").strip()
        actual_invocation = _office_claw_tool_env_value(
            target_tool, "OFFICE_CLAW_INVOCATION_ID"
        )
        active_allowed = get_active_office_claw_mcp_tool_ids()
        is_request_tool_without_binding = (
            target_tool_id.startswith(OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX)
            and not expected_invocation
            and self._office_claw_active_tool_ids is None
            and active_allowed is None
        )
        if is_request_tool_without_binding:
            logger.warning(
                "%s refuse OfficeClaw tool without request binding: "
                "tool_name=%s tool_id=%s actual_inv=%s",
                _LOG_PREFIX,
                tool_name,
                target_tool_id,
                (actual_invocation[:8] + "…") if actual_invocation else "-",
            )
            return {
                "success": False,
                "error": (
                    "OfficeClaw MCP tools are not bound for this request; "
                    "refusing cross-session invoke — please retry"
                ),
                "tool_name": tool_name,
            }
        if expected_invocation:
            if actual_invocation and actual_invocation != expected_invocation:
                active_id = resolve_active_office_claw_tool_id(tool_name) or owned_id
                recovered = (
                    self._lookup_tool_instance(active_id) if active_id else None
                )
                recovered_inv = (
                    _office_claw_tool_env_value(
                        recovered, "OFFICE_CLAW_INVOCATION_ID"
                    )
                    if recovered is not None
                    else ""
                )
                if recovered is not None and recovered_inv == expected_invocation:
                    logger.warning(
                        "%s replaced cross-request OfficeClaw tool instance: "
                        "tool_name=%s stale_id=%s active_id=%s",
                        _LOG_PREFIX,
                        tool_name,
                        target_tool_id,
                        active_id,
                    )
                    target_tool = recovered
                    target_tool_id = active_id or target_tool_id
                else:
                    logger.warning(
                        "%s refuse OfficeClaw tool credential mismatch: "
                        "tool_name=%s tool_id=%s expected_inv=%s actual_inv=%s",
                        _LOG_PREFIX,
                        tool_name,
                        target_tool_id,
                        expected_invocation[:8],
                        actual_invocation[:8],
                    )
                    return {
                        "success": False,
                        "error": (
                            "OfficeClaw MCP tool credentials belong to another "
                            "request; refusing cross-session invoke"
                        ),
                        "tool_name": tool_name,
                    }

        pinned_thread = str(self._office_claw_delivery_thread_id or "").strip()
        if (
            pinned_thread
            and tool_name in _OFFICE_CLAW_SCHEDULE_THREAD_PIN_TOOLS
        ):
            arguments["deliveryThreadId"] = pinned_thread

        try:
            kwargs_without_session = {k: v for k, v in kwargs.items() if k != "session"}
            owned = self._office_claw_active_tool_ids
            if owned is not None:
                kwargs_without_session[OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG] = owned
            call_timeout = getattr(AbilityManager, "_resolve_call_timeout")(
                target_tool_card
            )
            with self._bind_deepresearch_context(tool_name):
                try:
                    with anyio.fail_after(call_timeout) as timeout_scope:
                        result = await target_tool.invoke(
                            arguments, session=session, **kwargs_without_session
                        )
                except TimeoutError as exc:
                    if not timeout_scope.cancel_called:
                        raise
                    raise TimeoutError(
                        f"Tool '{tool_name}' timed out after {call_timeout}s"
                    ) from exc
            env_inv = _office_claw_tool_env_value(
                target_tool, "OFFICE_CLAW_INVOCATION_ID"
            )
            logger.info(
                "%s invoke tool=%s success=True result_type=%s tool_id=%s "
                "env_invocation_id=%s delivery_thread_id=%s",
                _LOG_PREFIX,
                tool_name,
                type(result).__name__,
                target_tool_id,
                (env_inv[:8] + "…") if env_inv else "-",
                pinned_thread or "-",
            )
            return {
                "success": True,
                "tool_name": tool_name,
                "result": _json_safe_value(result),
            }
        except Exception as exc:
            logger.warning(
                "%s invoke tool=%s failed: %s",
                _LOG_PREFIX,
                tool_name,
                exc,
                exc_info=True,
            )
            return {
                "success": False,
                "error": str(exc),
                "tool_name": tool_name,
            }
