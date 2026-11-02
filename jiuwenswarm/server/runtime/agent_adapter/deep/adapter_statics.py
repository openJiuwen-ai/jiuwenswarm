"""无 Code/Flash override 的 DeepAdapter staticmethod 抽出。

职责：
- browser / mcp / subagent 开关与若干 rail 轻量 builder
- ``shutdown_context_session_memory`` / ``supports_pending_reload``

不是新 rail；抽出后须在 ``JiuWenSwarmDeepAdapter`` 类上挂回
``staticmethod``，供 ``Class.xxx`` / ``getattr`` / ``patch.object`` 使用。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.rails import SecurityRail
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail

from jiuwenswarm.agents.harness.common.rails import (
    ContextOverflowRecoveryRail,
    JiuSwarmStreamEventRail,
    ResponsePromptRail,
)
from jiuwenswarm.agents.harness.common.rails.disabled_tools_rail import DisabledToolsRail
from jiuwenswarm.agents.harness.common.rails.eternal_conversation import (
    EternalConversationRail,
)
from jiuwenswarm.common.config import (
    get_config,
    is_subagent_runtime_enabled,
    resolve_string_or_list_config,
)
from jiuwenswarm.common.local_env_config import read_env
from jiuwenswarm.common.mcp_config import (
    build_mcp_server_config,
    extract_enabled_mcp_server_entries,
)
from jiuwenswarm.common.tool_ownership import mark_stateless, register_tool
from jiuwenswarm.server.runtime.agent_adapter.deep.adapter_types import (
    _MISSING_SESSION_MEMORY_MGR,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.agent_config_helpers import (
    _model_routing_enabled,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.model_factory import (
    _build_model_from_entry,
)

logger = logging.getLogger(__name__)


def shutdown_context_session_memory(context_processor_rail: ContextProcessorRail) -> bool:
    """Stop overlapping semantic session memory through a stable Adapter API.

    三态区分（CR-5），保证日志不失实：

    - manager 在位：shutdown 并置 None，返回 True；
    - 属性为 None：未配置（框架默认，仅在
      session_memory_compressor_config.enabled=true 时创建）或已
      关闭的合法幂等态，debug 即可，不告警；
    - 属性缺失（agent-core 私有属性被改名 / rail 类型不符）：
      保留 CR-2 加固的 WARNING——SessionMemoryManager 可能仍在
      eternal conversation 旁 unnoticed 运行（双记忆后端风险）。

    上游 02957a531 对两种非在位状态都是静默 False；缺失属性告警
    是本地有意的加强。
    """
    session_memory_manager = getattr(context_processor_rail, '_session_memory_mgr', _MISSING_SESSION_MEMORY_MGR)
    if session_memory_manager is _MISSING_SESSION_MEMORY_MGR:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] shutdown_context_session_memory found no "
            "_session_memory_mgr attribute on %r; the framework may have renamed it "
            "and SessionMemoryManager may keep running beside eternal conversation "
            "(double memory backend)",
            type(context_processor_rail).__name__,
        )
        return False
    if session_memory_manager is None:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] shutdown_context_session_memory: no active "
            "SessionMemoryManager on %r (not configured or already shut down)",
            type(context_processor_rail).__name__,
        )
        return False
    session_memory_manager.shutdown()
    setattr(context_processor_rail, '_session_memory_mgr', None)
    return True


def _build_model_routing(config: dict[str, Any] | None = None) -> Any | None:
    """构建 ModelRoutingRail（模型路由）。

        - 使能：config.yaml ``model_routing.enabled`` = true，或进程级 env
          ``JIUWENSWARM_MODEL_ROUTING_ENABLED``（relay spawn 注入，见 _model_routing_enabled）。
        - 始终真切换（apply_routing=True）。
        - 只对 auto 档生效：relay 置 ``params.enable_routing=True`` 时 rail 跑分类器，
          score >= 40 换 glm-5.3，否则保留 relay 下发的模型；具体模型名走 skip 分支。
        - 能力表来自 ``routing_state/models.json``（sidecar）或 config.yaml ``models.defaults``
          + ``models.vision``（后者作 model_type="vision" 候选）；model_builder 传
          _build_model_from_entry 使能力表带 Model 对象（真切换前置）。
        """
    if not _model_routing_enabled(config):
        return None
    try:
        from jiuwenswarm.agents.harness.common.rails.model_routing import (
            ModelRoutingRail,
            build_capability_table_from_config,
        )
        caps = build_capability_table_from_config(
            config, model_builder=_build_model_from_entry
        )
        rail = ModelRoutingRail(caps, apply_routing=True)
        logger.info('[JiuWenSwarmDeepAdapter] ModelRoutingRail create success, %d models', len(caps))
        return rail
    except Exception as exc:
        logger.warning('[JiuWenSwarmDeepAdapter] ModelRoutingRail create failed: %s', exc)
        return None


def _build_disabled_tools_rail(config: dict[str, Any]) -> DisabledToolsRail | None:
    """Build DisabledToolsRail to filter out disabled tools based on config.

        ``react.disabled_tools`` accepts two formats (same field, pick one):
          - YAML list:      ``disabled_tools: ["bash", "fork_agent"]``
          - Env var string: ``disabled_tools: ${DISABLED_TOOLS-defaults}``
            (set DISABLED_TOOLS=bash,fork_agent in .env)
        """
    try:
        disabled_list = resolve_string_or_list_config(config.get('disabled_tools'))
        if not disabled_list:
            return None
        rail = DisabledToolsRail(disabled_tools=disabled_list)
        logger.info('[JiuWenSwarmDeepAdapter] DisabledToolsRail create success, disabled_tools: %s', disabled_list)
        return rail
    except Exception as exc:
        logger.error('[JiuWenSwarmDeepAdapter] DisabledToolsRail create failed: %s', exc, exc_info=True)
        raise


def _register_agent_owned_tool(tool: Any, owner_id: str) -> Any:
    """Register a tool instance owned exclusively by this adapter's agent.

        ``_get_tool_cards`` runs before ``create_deep_agent``, so there is no
        AbilityManager to route through yet; :func:`register_tool` applies the
        contract it would have applied, which keeps two things true: rebuilding
        an agent rebinds the id to the fresh instance instead of failing as a
        duplicate, and the registration stays reclaimable by
        ``AbilityManager.teardown_tools`` on cleanup.

        A card that already declares itself shared wins over the call site: the
        two must agree or teardown would look for an id that was never
        registered, so the declaration on the card is the one that counts.

        Args:
            tool: Per-agent tool instance to register.
            owner_id: Owner id that this adapter's agent registers under.

        Returns:
            The registered tool instance.
        """
    if getattr(tool.card, "stateless", False):
        logger.debug(
            "[JiuWenSwarmDeepAdapter] tool %s is declared shared; registering it "
            "as shared despite the agent-owned call site",
            tool.card.name,
        )
    return register_tool(tool, owner_id)


def _register_shared_tool(tool: Any) -> Any:
    """Declare a tool instance shared across adapters, then register it.

        Shared here means one instance serves every adapter in the process: the
        bare id is kept and a repeated registration is an idempotent no-op, so
        the first registrant wins instead of adapters racing to replace each
        other. Two kinds of tool take this path — module-level ``@tool``
        singletons, and toolkits whose backing manager is deliberately
        process-wide (skills, symphony), where per-session instances would split
        state that is meant to be shared.

        Args:
            tool: Tool instance to share process-wide.

        Returns:
            The process-global registered tool instance (may be an earlier one).
        """
    mark_stateless([tool])
    return register_tool(tool, None)


def _build_request_summary_rail() -> Any | None:
    """Build RequestSummaryRail for per-request performance summaries."""
    try:
        from jiuwenswarm.perf.request_summary_rail import RequestSummaryRail
        rail = RequestSummaryRail(record_only=True)
        logger.info('[JiuWenSwarmDeepAdapter] RequestSummaryRail create success')
    except Exception as exc:
        logger.warning('[JiuWenSwarmDeepAdapter] RequestSummaryRail create failed: %s', exc)
        rail = None
    return rail


def _browser_runtime_enabled() -> bool:
    """Whether browser runtime support is enabled for DeepAgent subagent wiring."""
    value = str(
        read_env("PLAYWRIGHT_RUNTIME_MCP_ENABLED")
        or read_env("BROWSER_RUNTIME_MCP_ENABLED")
        or ""
    ).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _resolve_prompt_channel(session_id: str | None = None) -> str:
    """Resolve prompt channel from session id."""
    if not session_id:
        return 'web'
    channel = session_id.split('_', 1)[0]
    if channel == 'sess':
        return 'web'
    if channel in {'acp', 'cron', 'heartbeat', 'feishu', 'web', 'dingtalk', 'wecom', 'tui'}:
        return channel
    return 'web'


def _sync_browser_runtime_environment(config_base: dict[str, Any] | None = None) -> None:
    """Synchronize browser launch settings before browser runtimes are built."""
    from jiuwenswarm.agents.harness.common.browser_env import sync_browser_runtime_environment
    sync_browser_runtime_environment(config_base)


def _build_response_prompt_rail() -> ResponsePromptRail | None:
    """Build ResponsePromptRail so message rules keep priority ordering."""
    try:
        rail = ResponsePromptRail()
        logger.info('[JiuWenSwarmDeepAdapter] ResponsePromptRail create success')
    except Exception as exc:
        logger.warning('[JiuWenSwarmDeepAdapter] ResponsePromptRail create failed: %s', exc)
        rail = None
    return rail


def _build_stream_event_rail() -> JiuSwarmStreamEventRail | None:
    """Build JiuSwarmStreamEventRail."""
    try:
        stream_event_rail = JiuSwarmStreamEventRail()
        logger.info('[JiuWenSwarmDeepAdapter] JiuSwarmStreamEventRail create success')
    except Exception as exc:
        logger.warning('[JiuWenSwarmDeepAdapter] JiuSwarmStreamEventRail create failed: %s', exc)
        stream_event_rail = None
    return stream_event_rail


def _build_context_overflow_recovery_rail() -> ContextOverflowRecoveryRail | None:
    """Build ContextOverflowRecoveryRail."""
    try:
        recovery_rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
        logger.info('[JiuWenClawDeepAdapter] ContextOverflowRecoveryRail create success')
    except Exception as exc:
        logger.warning('[JiuWenClawDeepAdapter] ContextOverflowRecoveryRail create failed: %s', exc)
        recovery_rail = None
    return recovery_rail


def _build_security_rail() -> SecurityRail | None:
    """Build SecurityPromptRail."""
    try:
        security_prompt_rail = SecurityRail()
        logger.info('[JiuWenSwarmDeepAdapter] SecurityPromptRail create success')
    except Exception as exc:
        logger.warning('[JiuWenSwarmDeepAdapter] SecurityPromptRail create failed: %s', exc)
        security_prompt_rail = None
    return security_prompt_rail


def _build_eternal_conversation_rail() -> EternalConversationRail | None:
    """Mount an inert Rail; a Session request flag activates it later."""
    try:
        rail = EternalConversationRail()
        logger.info('[JiuWenSwarmDeepAdapter] EternalConversationRail created (disabled)')
        return rail
    except Exception as exc:
        logger.warning('[JiuWenSwarmDeepAdapter] EternalConversationRail create failed: %s', exc)
        return None


def _is_acp_tool_profile(config: dict[str, Any] | None = None) -> bool:
    if not isinstance(config, dict):
        return False
    tool_profile = str(config.get('tool_profile') or '').strip().lower()
    if tool_profile:
        return tool_profile == 'acp'
    channel_id = str(config.get('channel_id') or '').strip().lower()
    return channel_id == 'acp'


def _resolve_enable_subagent_runtime(config_base: dict[str, Any] | None = None) -> bool:
    """Return whether persistent subagent runtime tools should be enabled."""
    return is_subagent_runtime_enabled(config_base if config_base is not None else get_config())


def _is_subagent_default_enabled(subagent_cfg: Any) -> bool:
    """Default-enabled subagent: enabled unless explicitly set to false."""
    if not isinstance(subagent_cfg, dict):
        return True
    return subagent_cfg.get('enabled', True) is not False


def supports_pending_reload() -> bool:
    """Whether this adapter can defer reload while busy."""
    return True


def _is_subagent_enabled(subagent_cfg: Any) -> bool:
    """Treat only explicit `enabled: true` as enabled."""
    return isinstance(subagent_cfg, dict) and bool(subagent_cfg.get('enabled', False))


def _build_mcp_server_config(entry: dict[str, Any]) -> McpServerConfig | None:
    return build_mcp_server_config(entry, server_id_scope='jiuwenswarm')


def _extract_enabled_mcp_server_entries(config_base: dict[str, Any]) -> list[dict[str, Any]]:
    return extract_enabled_mcp_server_entries(config_base)
