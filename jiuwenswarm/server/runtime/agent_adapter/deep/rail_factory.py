"""按配置或环境变量构造不依赖适配器实例的 rail 和额外工具。"""

from __future__ import annotations
import hashlib
import importlib
import os
import threading
from typing import (
    Any,
    Callable,
)
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.rails import (
    SkillUseRail,
    SysOperationRail,
    HeartbeatRail,
)
from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import NotifyingLLMRetryRail
from jiuwenswarm.agents.harness.common.rails import TaskExecutionRail
from jiuwenswarm.agents.harness.common.rails.concurrent_safe_rails import ConcurrentSafeSysOperationRail
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.mcp_config import (
    MCP_REGISTRY_REQUEST_TOOL_ID_PREFIX,
    RequestScopedOfficeClawMcpTool,
    _positive_timeout_s,
)
from jiuwenswarm.common.schema.agent import AgentRequest
import logging

logger = logging.getLogger(__name__)

# Process-level cache for AGENT_EXTRA_TOOLS loads, keyed by module path
# (see _load_extra_tools_from_env): session-scoped adapters rebuild per
# session, and extra tools register process-shared under their card id,
# so the same module must resolve to the same instances on every build.
_EXTRA_TOOLS_CACHE: dict[str, list[Any]] = {}

_EXTRA_TOOLS_CACHE_LOCK = threading.Lock()

def _stage_request_mcp_tools(
    request: AgentRequest,
    server_name: str,
    tool_defs: list[dict[str, Any]],
    params: dict[str, Any],
    buffers: _RequestMcpToolBuffers,
    *,
    use_global_pool: bool = False,
    strict: bool = False,
    yield_to_existing: bool = False,
) -> None:
    """Build request tools without publishing a partially discovered request."""
    request_scope = hashlib.sha256(
        f"{request.session_id}:{request.request_id}".encode("utf-8")
    ).hexdigest()[:20]
    prefix = MCP_REGISTRY_REQUEST_TOOL_ID_PREFIX if use_global_pool else "office-claw-request-"
    for tool_def in tool_defs:
        name = str(tool_def.get("name") or "").strip()
        if not name or name in buffers.seen_names:
            if strict and (not name or not yield_to_existing):
                raise RuntimeError("OfficeClaw MCP returned an invalid or duplicate tool name")
            logger.warning(
                "[JiuWenSwarmDeepAdapter] skipping duplicate/invalid MCP tool: "
                "request_id=%s server=%s name=%s", request.request_id, server_name, name,
            )
            continue
        buffers.seen_names.add(name)
        card = ToolCard(
            id=f"{prefix}{request_scope}.{server_name}.{name}",
            name=name,
            description=str(tool_def.get("description") or ""),
            input_params=tool_def.get("input_params") or {},
        )
        if not strict:
            timeout = _positive_timeout_s(params.get("timeout_s"))
            if timeout is not None:
                card.properties["resilience"] = {"timeout_s": timeout}
        tool = RequestScopedOfficeClawMcpTool(
            card, params, request.request_id, server_name, use_global_pool=use_global_pool,
        )
        buffers.pending_tools.append((tool, strict))

def _office_claw_thread_id_from_tools(
    tools: list[RequestScopedOfficeClawMcpTool],
) -> str:
    for tool in tools:
        params = getattr(tool, "_params", None)
        if not isinstance(params, dict):
            continue
        env = params.get("env")
        if not isinstance(env, dict):
            continue
        thread_id = str(env.get("OFFICE_CLAW_THREAD_ID") or "").strip()
        if thread_id:
            return thread_id
    return ""

def _is_sandbox_bound_rail(rail: Any) -> bool:
    """动手类 rail：必须跟 deep_config / 沙箱 sysop，不能强制本地。"""
    if isinstance(rail, (SysOperationRail, SkillUseRail)):
        return True
    try:
        if isinstance(rail, ConcurrentSafeSysOperationRail):
            return True
    except Exception:
        pass
    return type(rail).__name__ in {
        "SysOperationRail",
        "FileSystemRail",
        "ConcurrentSafeFileSystemRail",
        "ConcurrentSafeSysOperationRail",
        "SkillUseRail",
    }

def _set_rail_sys_operation(rail: Any, sysop: SysOperation) -> bool:
    """给单个 rail 写入 sys_operation；成功返回 True。"""
    setter = getattr(rail, "set_sys_operation", None)
    if callable(setter):
        setter(sysop)
        return True
    if hasattr(rail, "sys_operation"):
        rail.sys_operation = sysop
        return True
    return False

def _build_task_execution_rail() -> TaskExecutionRail | None:
    """Build TaskExecutionRail for task.start/complete/update lifecycle events."""
    try:
        task_rail = TaskExecutionRail()
        logger.info("[JiuWenSwarmDeepAdapter] TaskExecutionRail create success")
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] TaskExecutionRail create failed: %s", exc)
        task_rail = None
    return task_rail

def _build_extension_config_debug_rail() -> Any | None:
    """Build ExtensionConfigDebugRail when explicitly enabled.

    Off by default. Enterprise only; set ``AGENT_EXTENSION_CONFIG_DEBUG_RAIL``
    to ``1`` / ``true`` / ``yes`` / ``on`` to mount the debug rail.
    """
    if not is_enterprise():
        return None
    from jiuwenswarm.agents.harness.common.rails.extension_config_util import (
        is_extension_config_debug_rail_enabled,
    )

    if not is_extension_config_debug_rail_enabled():
        return None
    try:
        from jiuwenswarm.agents.harness.common.rails.extension_config_debug_rail import (
            ExtensionConfigDebugRail,
        )
        rail = ExtensionConfigDebugRail()
        logger.info("[JiuWenSwarmDeepAdapter] ExtensionConfigDebugRail create success")
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] ExtensionConfigDebugRail create failed: %s", exc
        )
        rail = None
    return rail

def _load_extra_rails_from_env() -> list[Any]:
    """Load extra DeepAgentRails from AGENT_EXTRA_RAILS env var.

    Env format (semicolon-separated module paths):
        AGENT_EXTRA_RAILS=path.to.module1;path.to.module2

    Each module must expose a ``register_rails()`` function that returns
    a list of DeepAgentRail instances. Only honored under enterprise edition.
    """
    if not is_enterprise():
        return []
    env_value = os.getenv("AGENT_EXTRA_RAILS", "").strip()
    if not env_value:
        return []

    extra_rails: list[Any] = []
    for module_path in [p.strip() for p in env_value.split(";") if p.strip()]:
        try:
            mod = importlib.import_module(module_path)
            register_fn = getattr(mod, "register_rails", None)
            if register_fn is None:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] Extra rail module '%s' has no register_rails(), skipping",
                    module_path,
                )
                continue
            rails = register_fn()
            if rails:
                if not isinstance(rails, list):
                    rails = [rails]
                extra_rails.extend(rails)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] Loaded %d rail(s) from '%s'",
                    len(rails),
                    module_path,
                )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] Failed to load extra rails from '%s': %s",
                module_path,
                exc,
            )
    return extra_rails

def _load_extra_tools_from_env() -> list[Any]:
    """Load extra tools from AGENT_EXTRA_TOOLS env var.

    Env format (semicolon-separated module paths):
        AGENT_EXTRA_TOOLS=path.to.module1;path.to.module2

    Each module must expose a ``register_tools()`` function that returns
    a list of Tool instances. Only honored under enterprise edition.

    Results are cached per module for the process lifetime: session-scoped
    adapters rebuild per session, and extra tools register process-shared
    under their card id — without the cache, a factory returning fresh
    instances would register a new id on every build (registry leak,
    since stateless registrations are never torn down). Failures are not
    cached, so a transient failure retries on the next build.
    """
    if not is_enterprise():
        return []
    env_value = os.getenv("AGENT_EXTRA_TOOLS", "").strip()
    if not env_value:
        return []

    extra_tools: list[Any] = []
    for module_path in [p.strip() for p in env_value.split(";") if p.strip()]:
        try:
            # The lock spans cache lookup + import + factory call: concurrent
            # session builds must not race past a not-yet-written cache entry
            # and invoke register_tools() twice (double-checked locking cannot
            # close that gap when the factory runs between the checks).
            with _EXTRA_TOOLS_CACHE_LOCK:
                cached = _EXTRA_TOOLS_CACHE.get(module_path)
                if cached is None:
                    mod = importlib.import_module(module_path)
                    register_fn = getattr(mod, "register_tools", None)
                    if register_fn is None:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] Extra tool module '%s' has no register_tools(), skipping",
                            module_path,
                        )
                        continue
                    tools = register_fn()
                    if tools and not isinstance(tools, list):
                        tools = [tools]
                    cached = tools or []
                    _EXTRA_TOOLS_CACHE[module_path] = cached
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] Loaded %d tool(s) from '%s'",
                        len(cached),
                        module_path,
                    )
            extra_tools.extend(cached)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] Failed to load extra tools from '%s': %s",
                module_path,
                exc,
            )
    return extra_tools

def _build_heartbeat_rail() -> HeartbeatRail | None:
    """Build HeartbeatRail."""
    try:
        heartbeat_rail = HeartbeatRail()
        logger.info("[JiuWenSwarmDeepAdapter] HeartbeatRail create success")
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] HeartbeatRail create failed: %s", exc)
        heartbeat_rail = None
    return heartbeat_rail

def _build_avatar_rail() -> Any | None:
    """Build AvatarPromptRail for digital avatar mode."""
    try:
        from jiuwenswarm.agents.harness.common.rails.avatar_rail import AvatarPromptRail

        rail = AvatarPromptRail()
        logger.info("[JiuWenSwarmDeepAdapter] AvatarPromptRail create success")
        return rail
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] AvatarPromptRail create failed: %s", exc)
        return None

def _build_llm_retry_rail(config_base: dict[str, Any] | None = None) -> NotifyingLLMRetryRail | None:
    try:
        config_base = config_base or get_config()
        guard_cfg = config_base.get("execution_guard", {}) if isinstance(config_base, dict) else {}
        retry_cfg = guard_cfg.get("llm_retry_rail", {}) if isinstance(guard_cfg, dict) else {}
        if retry_cfg.get("enabled", False) is not True:
            logger.info("[JiuWenSwarmDeepAdapter] LLMRetryRail disabled by config")
            return None
        rail = NotifyingLLMRetryRail(
            max_retries=retry_cfg.get("max_retries", 2),
            repeat_min_pattern_chars=retry_cfg.get("repeat_min_pattern_chars", 2),
            repeat_max_pattern_chars=retry_cfg.get("repeat_max_pattern_chars", 64),
            repeat_min_count=retry_cfg.get("repeat_min_count", 6),
            repeat_min_total_chars=retry_cfg.get("repeat_min_total_chars", 160),
            repeat_window_chars=retry_cfg.get("repeat_window_chars", 1024),
            single_char_repeat_count=retry_cfg.get("single_char_repeat_count", 100),
            retry_transient_invoke_errors=retry_cfg.get("retry_transient_invoke_errors", True),
            notify_user_on_retry=retry_cfg.get("notify_user_on_retry", True),
            notify_user_on_exhausted=retry_cfg.get("notify_user_on_exhausted", True),
        )
        logger.info("[JiuWenSwarmDeepAdapter] NotifyingLLMRetryRail create success")
        return rail
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] LLMRetryRail create failed: %s", exc)
        return None

def _build_work_plan_approval_rail() -> Any | None:
    """构建 work plan 的审批 rail（``exit_plan_mode`` 即时弹窗）。"""
    try:
        from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
            PlanApprovalInterruptRail,
        )

        return PlanApprovalInterruptRail()
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] PlanApprovalInterruptRail create failed: %s", exc
        )
        return None

def _should_register_acp_runtime_tools(
    channel_id: str | None,
    request_id: str | None,
    session_id: str | None,
    has_runtime_capability: bool,
) -> bool:
    if channel_id != "acp":
        return False
    if not request_id or not session_id:
        return False
    return has_runtime_capability

def _build_skill_protocol_prompt_rail() -> Any | None:
    """构建 SkillProtocolPromptRail: 注入技能执行规范提示词。"""
    try:
        from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
            SkillProtocolPromptRail,
        )
        rail = SkillProtocolPromptRail()
        logger.info("[JiuWenSwarmDeepAdapter] SkillProtocolPromptRail create success")
        return rail
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] SkillProtocolPromptRail create failed: %s", exc)
        return None

def _suspend_subagent_permission_rails(subagent: Any) -> Callable[[], None]:
    """临时禁用 fallback 子代理继承的权限审批 rail，返回恢复函数。

    general-purpose 子代理与父 agent **共享同一个** PermissionInterruptRail 实例
    （factory 注入时按引用复制），直接改共享实例会污染父 agent 后续的权限审批，
    因此采用 suspend/restore：调用方在 spawn 结束后（finally 中）执行恢复。
    ``resolve_interrupt`` 首次工具调用时会用 ``host.get_permissions_snapshot``
    刷新配置覆盖本设置，因此该回调也需临时替换。
    """
    configured = (
        subagent.configured_rails()
        if hasattr(subagent, "configured_rails")
        else []
    )
    rail_names = [rail.__class__.__name__ for rail in configured or []]

    def _make_restore(
        rail: Any,
        saved_config: dict,
        host: Any,
        saved_snapshot: Any,
    ) -> Callable[[], None]:
        """构造恢复函数，捕获当前循环轮次 rail 的挂起状态。"""

        def _restore() -> None:
            try:
                rail.update_config(saved_config)
            except Exception:
                logger.warning(
                    "[spawn_fallback] failed to restore permission rail config",
                    exc_info=True,
                )
            if host is not None:
                host.get_permissions_snapshot = saved_snapshot

        return _restore

    def _make_disabled_snapshot(config: dict) -> Callable[[], dict]:
        """构造「原配置 + enabled=False」的权限快照回调。

        resolve_interrupt 刷新时以此覆盖引擎配置；返回残缺 dict 会把
        tools/file_guard 二次清空（同 update_config 整体替换语义），
        故必须携带完整挂起配置。
        """

        def _snapshot() -> dict:
            return dict(config)

        return _snapshot

    restore_steps: list[Callable[[], None]] = []
    for rail in configured or []:
        cls_name = rail.__class__.__name__
        if cls_name not in (
            "PermissionInterruptRail",
            "SkillAuthorizationPermissionRail",
            "SkillTurboPermissionRail",
        ):
            continue
        try:
            saved_config = dict(getattr(rail, "_static_config", None) or {})
            suspended_config = dict(saved_config)
            suspended_config["enabled"] = False
            host = getattr(rail, "_host", None)
            saved_snapshot = (
                host.get_permissions_snapshot if host is not None else None
            )

            rail.update_config(suspended_config)
            if host is not None:
                host.get_permissions_snapshot = _make_disabled_snapshot(suspended_config)
            restore_steps.append(_make_restore(rail, saved_config, host, saved_snapshot))
        except Exception:
            logger.warning(
                "[spawn_fallback] failed to suspend permission rail %s",
                cls_name,
                exc_info=True,
            )
    logger.info(
        "[spawn_fallback] subagent rails=%s suspended_permission_rails=%d",
        rail_names,
        len(restore_steps),
    )

    def restore() -> None:
        for step in restore_steps:
            step()

    return restore

def find_team_skill_rail(request_id: str, channel_id: str | None = None):
    """Find TeamSkillEvolutionRail that owns the given pending request_id."""
    try:
        from jiuwenswarm.agents.harness.team import (
            find_team_skill_rail_across_managers,
            get_team_manager,
        )
        rail = get_team_manager(channel_id).find_team_skill_rail_for_request(request_id)
        if rail is not None:
            return rail
        return find_team_skill_rail_across_managers(request_id)
    except Exception:
        return None
