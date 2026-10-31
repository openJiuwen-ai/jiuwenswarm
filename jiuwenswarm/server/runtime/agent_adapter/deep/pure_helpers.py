"""请求参数判断、用量汇总、目标历史和运行时上下文清理。"""

from __future__ import annotations
import asyncio
import json
import operator
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.sys_operation import (
    SysOperation,
    SysOperationCard,
)
from openjiuwen.harness import DeepAgentConfig
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload as _SkillTurboConfirmPayload
from openjiuwen.harness.schema.interaction import InputDispatchMode
from jiuwenswarm.agents.harness.common.tools.deepresearch import reset_deepresearch_route
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_fast_path import RewriteFastPathResult
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_html_followup import (
    RewriteHtmlTarget,
    target_from_commit_result,
)
from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import resolve_a2x_config
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    INTERRUPT_RESUME_SOURCES,
    SKILL_EVOLUTION_APPROVAL_SCHEMA,
)
from jiuwenswarm.agents.harness.common.rails.execution_guard import CircuitBreakerConfig
from jiuwenswarm.server.runtime.session.session_metadata import build_server_push_message
from jiuwenswarm.server.runtime.session.session_history import (
    append_history_record,
    load_history_records,
)
from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
    AutoRebuildJob,
    EvolutionPushContext,
    build_evolution_status_update,
    evolution_meta_from_params,
    push_evolution_published,
    push_evolution_status,
)
from jiuwenswarm.common.config import (
    get_config,
    get_evolution_review_trigger_enabled,
    get_skill_create_enabled,
)
from jiuwenswarm.perf.interface_hooks import record_deepresearch_sdk_token_usage
from jiuwenswarm.agents.harness.common.tools.deepresearch.usage import normalize_workflow_llm_token_usage
from jiuwenswarm.agents.harness.common.auto_harness.service import _HARNESS_PACKAGES_FILE
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_user_workspace_dir
import logging

logger = logging.getLogger(__name__)

# 进程内已完成卡一次性守卫：同 (session, goal) 只落盘一次；跨进程重复由磁盘检查兜底
_GOAL_COMPLETED_PERSISTED: ClassVar[set[tuple[str, str]]] = set()

def _session_adapter_key(session_id: str | None) -> str:
    sid = str(session_id or "").strip()
    return sid or "default"

def _enterprise_resource_id_from_request(request: AgentRequest | None) -> str:
    """Return the authenticated Agent resource id carried by a request."""
    if request is None:
        return ""
    from jiuwenswarm.common.request_identity import web_routing_identity

    metadata = request.metadata if isinstance(request.metadata, dict) else None
    identity = web_routing_identity(metadata)
    return str(identity.get("bot_id") or "").strip()

def _session_has_live_subagent_runtime(
    adapter: "JiuWenSwarmDeepAdapter",
    session_id: str,
) -> bool:
    """Return True when a session still has live subagent slots in use.

    A missing control is idle: dest-stable SDK and never-spawned parents
    have no ``_subagent_controls``. If ``capacity()`` raises, treat the
    session as occupied so TTL cannot evict an Adapter that may still
    hold children.
    """
    deep_agent = getattr(adapter, "_instance", None)
    if deep_agent is None:
        return False
    controls = getattr(deep_agent, "_subagent_controls", None) or {}
    if not isinstance(controls, dict):
        controls = {}
    control = controls.get(session_id)
    if control is None:
        return False
    try:
        return int(control.capacity().get("used", 0)) > 0
    except Exception:
        # Occupancy unknown: skip TTL eviction rather than drop live children.
        logger.warning(
            "[JiuWenSwarmDeepAdapter] subagent capacity probe failed; "
            "treating session as occupied: session_id=%s",
            session_id,
        )
        return True

def _session_adapter_lock_has_waiters(lock: asyncio.Lock) -> bool:
    # asyncio.Lock has no public waiter inspection; keep the lock if a reconnect is queued on it.
    waiters = getattr(lock, "_waiters", None)
    return any(not waiter.cancelled() for waiter in list(waiters or ()))

def _get_a2x_config(config_base: dict[str, Any]) -> dict[str, Any]:
    """Resolve A2X config from ``react.a2x_registry`` with safe defaults."""
    return resolve_a2x_config(config_base)

def _is_related_session(target_sid: str, other_sid: str) -> bool:
    """Return True when *other_sid* belongs to the same session tree as *target_sid*.

    Covers direct ancestor/descendant relationships only
    (e.g. ``A`` ↔ ``A_B``, ``A`` ↔ ``A_B_C``).  Siblings such as
    ``tui_a`` and ``tui_b`` are treated as unrelated — they are
    independent root sessions that share a channel-prefix convention,
    not sub-sessions of a common parent.
    """
    if not target_sid or not other_sid:
        return target_sid == other_sid
    if other_sid == target_sid:
        return True
    return other_sid.startswith(f"{target_sid}_") or target_sid.startswith(f"{other_sid}_")

def _resolve_managed_browser_binary_from_config(
    config_base: dict[str, Any] | None = None,
) -> str:
    """Resolve managed-browser binary from saved browser config."""
    from jiuwenswarm.agents.harness.common.browser_env import (
        resolve_managed_browser_binary,
    )

    return resolve_managed_browser_binary(config_base)

def _resolve_headless_from_config(
    config_base: dict[str, Any] | None = None,
) -> bool:
    """Read browser.headless from config (default True = headless)."""
    from jiuwenswarm.agents.harness.common.browser_env import resolve_headless

    return resolve_headless(config_base)

def _leftover_request_mcp_servers(
    request_mcp_servers: dict[str, dict[str, Any]] | None,
    covered_names: list[str],
) -> tuple[dict[str, dict[str, Any]] | None, list[str]]:
    """Drop servers already named in mcp_server_list; keep leftovers such as pptx-mcp."""
    if not request_mcp_servers:
        return None, []
    covered = set(covered_names)
    dropped = [name for name in request_mcp_servers if name in covered]
    leftover = {
        name: config for name, config in request_mcp_servers.items() if name not in covered
    }
    return (leftover or None), dropped

def _inject_request_id_into_inputs(
    request: AgentRequest, inputs: dict[str, Any]
) -> None:
    """把 ``request.request_id`` 注入 ``inputs["run"]["context"]["extra"]["request_id"]``。

    供 ``ModelRoutingRail.before_invoke`` 在 invoke 起点重绑日志的 ``[req=]``
    ContextVar。agent 执行跑在与 handler **不同的 task**（warm-pool / 长生命周期
    复用），其 ContextVar 可能是**上一个请求**捕获的陈旧值，导致 rail 决策日志
    带错 req 标签。这里把本请求 id 作为**数据**随 inputs 传入（与 model_selection /
    enable_routing 同一条通道，逐请求正确），rail 侧据此重绑即可修正标签。
    """
    rid = str(getattr(request, "request_id", "") or "").strip()
    if not rid:
        return
    run_extra = (
        inputs.setdefault("run", {})
        .setdefault("context", {})
        .setdefault("extra", {})
    )
    if isinstance(run_extra, dict):
        run_extra["request_id"] = rid

def _normalize_reload_value(value: Any) -> Any:
    """Normalize config-like values for stable hot-reload comparisons."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _normalize_reload_value(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_reload_value(item) for item in value]
    if isinstance(value, set):
        normalized_items = [_normalize_reload_value(item) for item in value]
        return sorted(normalized_items, key=repr)
    if hasattr(value, "model_dump"):
        try:
            return _normalize_reload_value(value.model_dump(mode="json"))
        except TypeError:
            return _normalize_reload_value(value.model_dump())
    if hasattr(value, "dict"):
        return _normalize_reload_value(value.dict())
    if hasattr(value, "value") and not isinstance(value, (bytes, bytearray)):
        return _normalize_reload_value(value.value)
    if hasattr(value, "__dataclass_fields__"):
        return _normalize_reload_value(
            {field: getattr(value, field) for field in value.__dataclass_fields__}
        )
    if hasattr(value, "__dict__"):
        return _normalize_reload_value(vars(value))
    return repr(value)

def _restore_omitted_reload_fields(
    deep_cfg: DeepAgentConfig,
    omitted_fields: dict[str, Any],
) -> None:
    for field_name, field_value in omitted_fields.items():
        setattr(deep_cfg, field_name, field_value)

def _inject_attribution_to_config(config: dict) -> None:
    """Inject OpenRouter attribution headers into all model_client_config entries in-place."""
    from jiuwenswarm.common.openrouter_attribution import inject_attribution_to_config
    inject_attribution_to_config(config)

def _sys_operation_isolation_key(sysop_card: SysOperationCard) -> str | None:
    try:
        sys_operation = SysOperation(sysop_card)
        return sys_operation.isolation_key_template
    except Exception as exc:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] failed to resolve sys_operation isolation key: %s",
            exc,
        )
        return None

def _get_registered_sys_operation_by_isolation_key(
    isolation_key_template: str | None,
) -> SysOperation | None:
    if not isolation_key_template:
        return None

    try:
        resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
        if resource_registry is None:
            return None
        sys_operation_mgr = resource_registry.sys_operation()
        owner_map = getattr(sys_operation_mgr, "_sandbox_key_owner_map", {})
        existing_op_id = owner_map.get(isolation_key_template)
        if not existing_op_id:
            return None
        return Runner.resource_mgr.get_sys_operation(existing_op_id)
    except Exception as exc:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] failed to get registered sys_operation: %s",
            exc,
        )
        return None

def _get_active_package_config_paths() -> list[str]:
    """Read harness-packages.json to get config_path from active packages.

    Returns:
        List of harness_config.yaml paths from active packages.
    """
    config_paths: list[str] = []
    try:
        if not _HARNESS_PACKAGES_FILE.exists():
            return config_paths

        data = json.loads(_HARNESS_PACKAGES_FILE.read_text(encoding="utf-8"))
        active_ids = data.get("active_package_ids", [])
        if not active_ids:
            return config_paths

        for pkg in data.get("packages", []):
            pkg_id = pkg.get("id", "")
            if pkg_id not in active_ids:
                continue

            config_path = pkg.get("config_path", "")
            if not config_path:
                continue

            config_file = Path(config_path)
            if config_file.exists():
                config_paths.append(str(config_file))
                logger.info(
                    "[JiuWenSwarmDeepAdapter] Found active package config: %s (package=%s)",
                    config_path,
                    pkg_id,
                )
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] Failed to read active packages config paths: %s",
            exc,
        )

    return config_paths

def _resolve_skill_sleep_traces_dir() -> Path:
    """Resolve OTel file-exporter traces dir for skill sleep harvest."""
    try:
        full = get_config()
    except Exception:
        full = {}
    if not isinstance(full, dict):
        full = {}
    for key in ("agent_observability", "team_observability"):
        block = full.get(key)
        if isinstance(block, dict):
            raw = str(block.get("traces_dir") or "").strip()
            if raw:
                return Path(raw).expanduser()
    return get_user_workspace_dir() / ".trace"

async def _push_skill_sleep_published(
    *,
    skill_name: str,
    version: str,
    session_id: str,
    request_id: str,
) -> None:
    """AOM ``published`` push after an offline sleep cycle auto-adopts a skill."""
    from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

    push_context = EvolutionPushContext(
        transport=WebSocketGatewayPushTransport(),
        channel_id=None,
        session_id=session_id,
    )
    await push_evolution_published(
        push_context,
        request_id=request_id,
        skill_name=skill_name,
        version=version,
        build_push_message=build_server_push_message,
        source="skill_sleep",
    )
    logger.info(
        "[JiuWenSwarmDeepAdapter] skill sleep published push: skill=%s version=%s "
        "session=%s request_id=%s",
        skill_name,
        version,
        session_id,
        request_id,
    )

async def _push_skill_sleep_suggest(
    *,
    skill_name: str,
    session_id: str,
    request_id: str,
) -> None:
    """Push ``chat.evolution_status`` end/completed so relay can notify suggest."""
    from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

    push_context = EvolutionPushContext(
        transport=WebSocketGatewayPushTransport(),
        channel_id=None,
        session_id=session_id,
    )
    await push_evolution_status(
        push_context,
        build_evolution_status_update(
            request_id=request_id,
            status="end",
            stage="completed",
            message=f"Skill sleep suggest completed for {skill_name}",
        ),
        build_server_push_message,
    )
    logger.info(
        "[JiuWenSwarmDeepAdapter] skill sleep suggest push: skill=%s "
        "session=%s request_id=%s",
        skill_name,
        session_id,
        request_id,
    )

def _ttse_consult_knobs(ttse_cfg: dict[str, Any]) -> tuple[int, str]:
    """Parse live ``consult_top_k`` / ``consult_retrieve_mode`` from yaml."""
    try:
        consult_top_k = int(ttse_cfg.get("consult_top_k", 8) or 8)
    except (TypeError, ValueError):
        consult_top_k = 8
    if consult_top_k <= 0:
        consult_top_k = 8
    try:
        from openjiuwen.agent_evolving.ttse.config import (
            normalize_consult_retrieve_mode,
        )
    except ImportError:
        def normalize_consult_retrieve_mode(value: Any) -> str:
            raw = str(value or "").strip().lower()
            return raw if raw in ("hybrid", "embed", "bm25") else "hybrid"

    return consult_top_k, normalize_consult_retrieve_mode(
        ttse_cfg.get("consult_retrieve_mode")
    )

def _coerce_circuit_breaker_threshold(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed

def _circuit_breaker_config_from_mapping(
    cb_cfg: dict,
    defaults: CircuitBreakerConfig,
) -> CircuitBreakerConfig | None:
    warning = _coerce_circuit_breaker_threshold(
        cb_cfg.get("warning_threshold", defaults.warning_threshold),
        defaults.warning_threshold,
    )
    critical = _coerce_circuit_breaker_threshold(
        cb_cfg.get("critical_threshold", defaults.critical_threshold),
        defaults.critical_threshold,
    )
    global_breaker = _coerce_circuit_breaker_threshold(
        cb_cfg.get("global_breaker_threshold", defaults.global_breaker_threshold),
        defaults.global_breaker_threshold,
    )
    unknown = _coerce_circuit_breaker_threshold(
        cb_cfg.get("unknown_tool_threshold", defaults.unknown_tool_threshold),
        defaults.unknown_tool_threshold,
    )
    non_retryable = _coerce_circuit_breaker_threshold(
        cb_cfg.get("non_retryable_threshold", defaults.non_retryable_threshold),
        defaults.non_retryable_threshold,
    )
    if warning > critical:
        return None
    if critical > global_breaker:
        return None
    return CircuitBreakerConfig(
        warning_threshold=warning,
        critical_threshold=critical,
        global_breaker_threshold=global_breaker,
        unknown_tool_threshold=unknown,
        non_retryable_threshold=non_retryable,
    )

def _resolve_enable_task_loop(
    config: dict[str, Any], config_base: dict[str, Any] | None
) -> bool:
    """Resolve enable_task_loop considering evolution rail requirements.

    SkillCreateRail and review-trigger SkillEvolutionRail follow-ups require
    task-loop mode (enable_task_loop=True) because they use
    AFTER_TASK_ITERATION events and enqueue_follow_up().
    When skill_create=True or review_trigger=True, we force enable_task_loop=True
    regardless of user config.

    Args:
        config: The react config section.
        config_base: The full config base (contains evolution.skill_create).

    Returns:
        True if task-loop should be enabled, False otherwise.
    """
    config_base = config_base or get_config()
    skill_create_enabled = get_skill_create_enabled(config_base)
    evolution_review_trigger_enabled = get_evolution_review_trigger_enabled(config_base)
    configured_value = config.get("enable_task_loop", True)

    if skill_create_enabled:
        if not configured_value:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] skill_create=True requires enable_task_loop=True; "
                "overriding user config (enable_task_loop=%s -> True)",
                configured_value,
            )
        return True
    if evolution_review_trigger_enabled:
        if not configured_value:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] evolution.review_trigger=True requires "
                "enable_task_loop=True; overriding user config (enable_task_loop=%s -> True)",
                configured_value,
            )
        return True
    return configured_value

def _reset_deepresearch_route_context(tokens: _RuntimeCronContextTokens) -> None:
    """Reset one DeepResearch route token at most once."""
    route_token = tokens.deepresearch
    if route_token is None or route_token.token is None:
        return
    token = route_token.token
    reset_deepresearch_route(token)
    route_token.token = None

def _run_cleanup_steps(
    steps: list[tuple[str, Callable[[], None]]],
) -> BaseException | None:
    """Run every cleanup step, returning the first failure without secrets."""
    first_error: BaseException | None = None
    for label, cleanup in steps:
        try:
            cleanup()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            logger.warning(
                "[JiuWenSwarmDeepAdapter] cleanup failed: step=%s error_type=%s",
                label,
                type(exc).__name__,
            )
    return first_error

def _reset_runtime_cron_context(
    tokens: _RuntimeCronContextTokens,
    *,
    suppress_errors: bool = False,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            _CRON_TOOL_BOUND,
            _CRON_TOOL_CHANNEL_ID,
            _CRON_TOOL_METADATA,
            _CRON_TOOL_MODE,
            _CRON_TOOL_SESSION_ID,
            _RUNTIME_TOOL_A2A_POLICY_ID,
            _RUNTIME_TOOL_RESOURCE_ID,
        )
    from openjiuwen.core.sys_operation.shell_process_registry import (
        reset_shell_session_id,
    )

    first_error: BaseException | None = None

    def _reset(name: str, reset: Callable[[Any], None]) -> None:
        nonlocal first_error
        token = getattr(tokens, name)
        if token is None:
            return
        try:
            reset(token)
        except BaseException as exc:  # cleanup must continue through cancellation
            if first_error is None:
                first_error = exc
        else:
            setattr(tokens, name, None)

    route_token = tokens.deepresearch
    if route_token is not None and route_token.token is not None:
        token = route_token.token
        try:
            reset_deepresearch_route(token)
        except BaseException as exc:
            first_error = exc
        else:
            route_token.token = None
    if tokens.send_file is not None:
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
            reset_send_file_request_context,
        )

        _reset("send_file", reset_send_file_request_context)
    _reset("shell", reset_shell_session_id)
    _reset("bound", _CRON_TOOL_BOUND.reset)
    _reset("a2a_policy_id", _RUNTIME_TOOL_A2A_POLICY_ID.reset)
    _reset("resource_id", _RUNTIME_TOOL_RESOURCE_ID.reset)
    _reset("mode", _CRON_TOOL_MODE.reset)
    _reset("metadata", _CRON_TOOL_METADATA.reset)
    _reset("session", _CRON_TOOL_SESSION_ID.reset)
    _reset("channel", _CRON_TOOL_CHANNEL_ID.reset)
    if first_error is not None and not suppress_errors:
        raise first_error

def _acp_runtime_tools_enabled(
    request_metadata: dict[str, Any] | None,
) -> tuple[bool, bool]:
    caps = (
        dict(request_metadata.get("acp_client_capabilities") or {})
        if isinstance(request_metadata, dict)
        else {}
    )
    logger.info(
        "[ACP] _acp_runtime_tools_enabled: metadata_keys=%s caps=%s",
        list((request_metadata or {}).keys()),
        caps,
    )

    fs_raw = caps.get("fs")
    if fs_raw is True:
        fs_enabled = True
    elif isinstance(fs_raw, dict):
        fs_enabled = bool(fs_raw.get("readTextFile") or fs_raw.get("writeTextFile"))
    else:
        fs_enabled = False

    terminal_raw = caps.get("terminal")
    if terminal_raw is True:
        terminal_enabled = True
    elif isinstance(terminal_raw, dict):
        terminal_enabled = bool(
            terminal_raw.get("create")
            or terminal_raw.get("output")
            or terminal_raw.get("waitForExit")
            or terminal_raw.get("release")
        )
    else:
        terminal_enabled = False

    return fs_enabled, terminal_enabled

def _extract_request_system_prompt(request: AgentRequest | None) -> str | None:
    """Read non-empty request.params.system_prompt."""
    if request is None or not isinstance(request.params, dict):
        return None
    raw = request.params.get("system_prompt")
    if isinstance(raw, str):
        value = raw.strip()
        if value:
            return value
    return None

def _extract_request_interactive_ask(request: AgentRequest | None) -> bool:
    """Guided mode is params.interactive_ask opt-in; never infer from HITL capability."""
    from jiuwenswarm.server.runtime.skill_turbo.interactive_ask import (
        extract_interactive_ask,
    )

    if request is None:
        return False
    params = request.params if isinstance(getattr(request, "params", None), dict) else None
    metadata = request.metadata if isinstance(getattr(request, "metadata", None), dict) else None
    return extract_interactive_ask(params, metadata)

def _resolve_eternal_conversation_enabled(params: Any) -> bool:
    """Resolve the V1 frontend runtime flag; absent remains disabled."""
    if not isinstance(params, dict):
        return False
    value = params.get("eternal_conversation_enabled", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)

def _is_eternal_interaction_resume(params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    # Single source of truth with the HITL resume dispatch: every source the
    # framework treats as an interrupt resume (permission/confirm/ask_user/
    # evolution) must resume the suspended natural task instead of starting
    # a new one, otherwise ask_user answers would split one natural task in
    # the eternal-conversation evidence chain.
    return str(params.get("source") or "").strip() in INTERRUPT_RESUME_SOURCES

def _cron_session_has_context(inner_session: Any) -> bool:
    """检测 AgentSession 的 global_state['context'] 是否已带 messages。

    跨 channel（cron 写 / web 读）恢复前用来判断当前 web adapter 的 session 是否
    已经持有 cron 历史——有则跳过 recover，无则强制从 checkpoint 重新恢复。
    """
    try:
        _state = inner_session.state().get_state(copied=False)
        if not isinstance(_state, dict):
            return False
        _ctx = (_state.get("global_state") or {}).get("context")
        if not isinstance(_ctx, dict):
            return False
        for _cst in _ctx.values():
            _msgs = _cst.get("messages") if isinstance(_cst, dict) else None
            if _msgs:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False

def _cron_execution_checkpointer(inner_session: Any) -> Any:
    """cron 落盘用的是 session 自己的 checkpointer，恢复必须读同一份。"""
    getter = getattr(inner_session, "checkpointer", None)
    if callable(getter):
        checkpointer = getter()
        if checkpointer is not None:
            return checkpointer
    return CheckpointerFactory.get_checkpointer()

def _has_uploaded_file(params: dict) -> bool:
    """Whether the request params carry a user-uploaded file (new content)."""
    try:
        files = params.get("files") or params.get("files_updated_by_user")
        if not files:
            return False
        import json as _json
        if isinstance(files, str):
            files = _json.loads(files)
        if isinstance(files, list):
            return len(files) > 0
        if isinstance(files, dict):
            return bool(files.get("files"))
    except Exception:
        pass
    return False

def _new_usage_accumulator() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_tokens": 0,
        "input_cost": 0.0,
        "output_cost": 0.0,
        "total_cost": 0.0,
    }

def _extract_usage_metadata(payload: Any) -> dict[str, Any] | None:
    """Pull GLM/OpenAI usage numbers out of a SkillTurbo or adapter payload."""
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("event_type") or "")
    if event_type == "chat.usage_metadata":
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            inner = metadata.get("usage_metadata", metadata)
            return inner if isinstance(inner, dict) else None
        return None
    if event_type in {"chat.llm_usage", "llm_usage"}:
        inner = payload.get("usage_metadata")
        return inner if isinstance(inner, dict) else None
    return None

def _accumulate_usage(acc: dict[str, Any], usage_meta: dict[str, Any]) -> None:
    for token in ("input_tokens", "output_tokens", "total_tokens", "cache_tokens"):
        acc[token] += usage_meta.get(token, 0) or 0
    for cost in ("input_cost", "output_cost", "total_cost"):
        acc[cost] += usage_meta.get(cost, 0.0) or 0.0

def _extract_deepresearch_sdk_usage(
    payload: Any,
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    result_info = payload.get("tool_result", payload)
    if not isinstance(result_info, dict):
        return None
    tool_name = result_info.get("tool_name") or result_info.get("name")
    if tool_name != "deepresearch_execute":
        return None
    raw_output = result_info.get("raw_output")
    if raw_output is None:
        raw_output = result_info.get("rawOutput")
    if raw_output is None:
        raw_output = result_info.get("result")
    if isinstance(raw_output, str):
        try:
            raw_output = json.loads(raw_output)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw_output, dict) or raw_output.get("kind") != "completed":
        return None
    usage = normalize_workflow_llm_token_usage(
        raw_output.get("workflow_llm_token_usage")
    )
    if usage is None or max(
        usage["total_tokens"],
        usage["input_tokens"] + usage["output_tokens"],
    ) <= 0:
        return None
    state = raw_output.get("state")
    conversation_id = (
        raw_output.get("conversation_id")
        or (state.get("conversation_id") if isinstance(state, dict) else None)
        or result_info.get("tool_call_id")
        or result_info.get("toolCallId")
    )
    usage_id = str(conversation_id or "").strip()
    if not usage_id:
        return None
    return usage_id, usage

def _account_deepresearch_sdk_usage(
    payload: Any,
    *,
    request_id: str,
    usage_accumulator: dict[str, Any],
    accounted_usage_ids: set[str],
) -> bool:
    extracted = _extract_deepresearch_sdk_usage(payload)
    if extracted is None:
        return False
    usage_id, usage = extracted
    if usage_id in accounted_usage_ids:
        return False
    accounted_usage_ids.add(usage_id)
    _accumulate_usage(usage_accumulator, usage)
    record_deepresearch_sdk_token_usage(request_id, usage_id, usage)
    return True

def _format_llm_usage_summary(usage_accumulator: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "input_tokens": usage_accumulator["input_tokens"],
        "output_tokens": usage_accumulator["output_tokens"],
        "total_tokens": usage_accumulator["total_tokens"],
    }
    input_tokens = usage_accumulator["input_tokens"]
    if input_tokens > 0:
        cache_tokens = usage_accumulator["cache_tokens"]
        summary["cache_tokens"] = cache_tokens
        summary["cache_hit_rate"] = f"{cache_tokens / input_tokens:.1%}"
    if usage_accumulator["input_cost"] > 0:
        summary["input_cost"] = round(usage_accumulator["input_cost"], 6)
    if usage_accumulator["output_cost"] > 0:
        summary["output_cost"] = round(usage_accumulator["output_cost"], 6)
    if usage_accumulator["total_cost"] > 0:
        summary["total_cost"] = round(usage_accumulator["total_cost"], 6)
    return summary

def _skill_turbo_answers_to_confirm_payload(
    answers: list,
    resume_ctx: dict[str, Any],
) -> Any:
    """将前端 answers 转为 ConfirmPayload（权限审批）或结构化 answers（ask_user）。

    结构化 ask_user 中断（SkillTurboAskUserRail）：前端 ask_user_question 卡片
    提交 ``{question, selected_options, custom_input}``，原样返回给 rail 解析；
    权限审批中断：保持原逻辑转为 ConfirmPayload。
    """
    if answers and all(
        isinstance(a, dict) and ("question" in a or "selected_options" in a)
        for a in answers
    ):
        return answers
    text = ""
    for ans in answers:
        if isinstance(ans, dict):
            a = ans.get("answer") or ans.get("value") or ""
            if a:
                text = str(a).strip()
                break
    if not text and answers:
        first = answers[0]
        if isinstance(first, str):
            text = first
    if text == "拒绝":
        return _SkillTurboConfirmPayload(approved=False, feedback="user rejected")
    if text == "总是允许":
        return _SkillTurboConfirmPayload(
            approved=True, auto_confirm=True, persist_allow=True
        )
    return _SkillTurboConfirmPayload(approved=True)

def _select_registered_runtime_tool_names(
    runtime_tool_candidates: tuple[str, ...],
    ability_names: set[str],
) -> list[str]:
    selected_names: list[str] = []
    for name in runtime_tool_candidates:
        if name in ability_names:
            selected_names.append(name)
    return selected_names

def _resolve_interrupt_session_id(session_id: str | None) -> str:
    return (session_id or "default").strip() or "default"

def _append_cancelled_tools_to_history(
    request: AgentRequest,
    cancelled_tool_results: list[dict[str, Any]],
) -> None:
    """Persist cancelled tool results so refresh does not leave spinners."""
    if not cancelled_tool_results:
        return
    mode = (
        request.params.get("mode", "unknown")
        if isinstance(request.params, dict)
        else "unknown"
    )
    for tool_info in cancelled_tool_results:
        append_history_record(
            session_id=request.session_id,
            request_id=request.request_id,
            channel_id=request.channel_id,
            role="assistant",
            event_type="chat.tool_result",
            content=tool_info.get("result", ""),
            timestamp=time.time(),
            extra={
                "tool_result": {
                    "tool_name": tool_info.get("tool_name", ""),
                    "tool_call_id": tool_info.get("tool_call_id", ""),
                    "result": tool_info.get("result", ""),
                    "status": tool_info.get("status", "error"),
                },
            },
            mode=mode,
        )

def _approved_plan_exit_resume_tool_call_id(params: Any) -> str:
    """返回已批准 plan-exit resume 对应的工具调用 ID。

    Confirm interrupt 恢复后，运行时可能在模型已经给出结论的情况下只向
    输出流暴露工具事件。这一标记让流末尾能为该极窄路径补一个用户可见的
    完成消息；其他 confirm / permission resume 不受影响。
    """
    if not isinstance(params, dict):
        return ""
    if str(params.get("source") or "").strip() != "confirm_interrupt":
        return ""
    tool_call_id = str(params.get("request_id") or "").strip()
    answers = params.get("answers")
    if not tool_call_id or not isinstance(answers, list) or not answers:
        return ""
    answer = answers[0]
    if not isinstance(answer, dict):
        return ""
    options = answer.get("selected_options")
    if not isinstance(options, list) or not options:
        return ""
    approved_values = {"approve", "本次允许", "批准", "proceed", "开始执行"}
    selected = str(options[0] or "").strip().lower()
    return tool_call_id if selected in approved_values else ""

def _should_fill_reasoning_only_empty_final(
    parsed: dict[str, Any] | None,
    *,
    had_reasoning_output: bool,
    visible_text_since_last_final: bool,
) -> bool:
    """Whether an empty chat.final should get the reasoning-only short reply."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("event_type") != "chat.final":
        return False
    if str(parsed.get("content") or "").strip():
        return False
    if not had_reasoning_output:
        return False
    return not visible_text_since_last_final

def _apply_reasoning_only_empty_reply_fallback(
    *,
    has_streamed_content: bool,
    had_reasoning_output: bool,
    fallback_content: str,
    reasoning_only_fallback: str,
) -> str:
    """Fill empty terminal final content for reasoning-only completions.

    Skip the generic fill when this stream already forwarded visible
    ``chat.delta`` text — an empty final is then only a trailer.
    """
    if str(fallback_content or "").strip():
        # Preserve dedicated fallbacks (e.g. plan-exit) over the generic text.
        return fallback_content
    if has_streamed_content:
        return fallback_content
    if not had_reasoning_output:
        return fallback_content
    return reasoning_only_fallback

def _resolve_input_dispatch_mode(params: Any) -> InputDispatchMode | None:
    """Map host ``input_mode`` / ``runtime_mode`` onto OpenJiuwen dispatch mode.

    Missing or unknown values keep pre-Goal Gateway replace semantics
    (``None`` → OpenJiuwen default FOLLOW_UP boundary when idle).
    """
    if not isinstance(params, dict):
        return None
    input_mode = str(
        params.get("input_mode") or params.get("runtime_mode") or ""
    ).strip().lower()
    if input_mode == "follow_up":
        return InputDispatchMode.FOLLOW_UP
    if input_mode == "steer":
        return InputDispatchMode.STEER
    return None

def _wants_attach_goal(params: Any) -> bool:
    return isinstance(params, dict) and params.get("attach_goal") is True

def _should_parse_tui_goal_slash(
    *,
    pending_goal_op: dict[str, Any] | None,
    attach_goal_request: bool,
    channel_id: Any,
    query: Any,
) -> bool:
    """Whether to parse chat text ``/goal ...`` (TUI only, when no structured op)."""
    if pending_goal_op is not None or attach_goal_request:
        return False
    if str(channel_id or "").strip().lower() != "tui":
        return False
    return isinstance(query, str)

def _parse_goal_slash_intent(query: str) -> dict[str, Any] | None:
    """Parse ``/goal ...`` into an action dict without touching GoalManager."""
    text = query.strip()
    if not text.startswith("/goal"):
        return None
    args = text[5:].strip()
    if not args:
        return {"action": "get"}
    lower = args.lower()
    if lower in {"pause", "resume", "clear"}:
        return {"action": lower}
    if lower.startswith("set "):
        return {"action": "set", "objective": args[4:].strip()}
    if lower == "set":
        return {"action": "set", "objective": ""}
    return {"action": "set", "objective": args}

def _is_subagent_approval_answer(request_id: str, params: Any) -> bool:
    """子代理委托审批应答：显式 source 或 request_id 前缀（兼容旧通道）。"""
    source = str(params.get("source") or "").strip() if isinstance(params, dict) else ""
    if source in {"subagent_skill_load", "subagent_tool_permission"}:
        return True
    return isinstance(request_id, str) and request_id.startswith(
        ("subagent_skill_load_", "subagent_tool_permission_")
    )

def _resolve_subagent_approval_answer(
    request: AgentRequest,
    request_id: str,
    answers: Any,
) -> bool:
    """把子代理委托审批应答路由回 SubagentApprovalRegistry 的 pending Future。"""
    params = request.params if isinstance(request.params, dict) else {}
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.runtime import (
            resolve_subagent_approval,
        )

        return resolve_subagent_approval(
            request_id=request_id,
            session_id=str(request.session_id or params.get("session_id") or ""),
            source=str(params.get("source") or ""),
            answers=answers,
            agent_scope_id=str(params.get("agent_scope_id") or ""),
        )
    except Exception:  # noqa: BLE001 — 应答路由失败不掩盖 user_answer 响应
        logger.warning(
            "[JiuWenSwarmDeepAdapter] subagent approval resolve failed: request_id=%s",
            request_id,
            exc_info=True,
        )
        return False

def _is_interrupt_resume_dispatch(params: Any) -> bool:
    """HITL answers must inject into the existing interaction when possible.

    Permission / confirm / ask-user resumes arrive as ``chat.send`` with
    ``answers``.  When a Goal (or other) consumer already holds the output
    lease, ``attach_output`` returns ``None`` — we must still ``send_input``
    so InteractiveInput reaches DeepAgent; otherwise the tool stays blocked
    while Goal keeps running.
    """
    if not isinstance(params, dict):
        return False
    source = str(params.get("source") or "").strip()
    answers = params.get("answers")
    request_id = str(params.get("request_id") or "").strip()
    if (
        bool(request_id)
        and isinstance(answers, list)
        and source in {
            "ask_user_interrupt",
            "confirm_interrupt",
            "permission_interrupt",
            "evolution_interrupt",
        }
    ):
        return True
    from jiuwenswarm.gateway.message_handler.evolution_approval import (
        is_interrupt_evolution_approval_answer_payload,
    )

    return is_interrupt_evolution_approval_answer_payload(params)

def _structured_goal_op_from_request(
    request: AgentRequest,
) -> dict[str, Any] | None:
    """Map streaming ``command.goal`` set/resume onto the attach→control path.

    Plain chat text like ``/goal set ...`` is never parsed here — only an
    explicit ``command.goal`` method (Web/TUI structured API).
    """
    if request.req_method != ReqMethod.COMMAND_GOAL:
        return None
    raw = request.params if isinstance(request.params, dict) else {}
    action = str(raw.get("action", "get") or "get").strip().lower()
    if action not in {"set", "resume"}:
        return None
    op: dict[str, Any] = {"action": action}
    if action == "set":
        objective = raw.get("objective")
        op["objective"] = objective if isinstance(objective, str) else ""
        op["overwrite_confirmed"] = bool(raw.get("overwrite_confirmed", False))
        for key in ("token_budget", "max_attempts"):
            value = raw.get(key)
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, int):
                op[key] = value
            elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
                op[key] = int(value.strip())
    return op

def _is_interrupt_skill_evolution_approval_params(request_id: str, params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if isinstance(request_id, str) and request_id.startswith("call_"):
        return _is_regular_skill_evolution_approval_params(params)
    evolution_meta = evolution_meta_from_params(params)
    return (
        evolution_meta.get("approval_transport") == "interrupt"
        and _is_regular_skill_evolution_approval_params(params)
    )

def _is_regular_skill_evolution_approval_params(params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if params.get("source") == "skill_evolution_approval":
        return True
    if params.get("approval_schema") == SKILL_EVOLUTION_APPROVAL_SCHEMA:
        return True
    approval_detail = params.get("approval_detail")
    if (
        isinstance(approval_detail, dict)
        and approval_detail.get("schema") == SKILL_EVOLUTION_APPROVAL_SCHEMA
    ):
        return True
    evolution_meta = evolution_meta_from_params(params)
    if evolution_meta.get("event_kind") != "approval":
        return False
    approval_kind = evolution_meta.get("approval_kind")
    rail_kind = evolution_meta.get("rail_kind")
    return approval_kind in (None, "", "evolve") and rail_kind in (None, "", "regular")

async def _push_team_skill_evolve_resolution_status(
    request_id: str,
    *,
    session_id: str | None,
    channel_id: str | None,
    accepted: bool,
) -> None:
    """Close the frontend evolution status after a team skill approval is resolved."""
    if not session_id:
        return
    from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

    stage = "completed" if accepted else "hidden"
    message = (
        "Team skill evolution accepted"
        if accepted
        else "Team skill evolution rejected"
    )
    try:
        await push_evolution_status(
            EvolutionPushContext(
                transport=WebSocketGatewayPushTransport(),
                channel_id=channel_id,
                session_id=session_id,
            ),
            build_evolution_status_update(
                request_id=request_id,
                status="end",
                stage=stage,
                message=message,
            ),
            build_server_push_message,
        )
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] team skill evolve status push failed: request_id=%s error=%s",
            request_id,
            exc,
        )

def _format_approval_summary(
    *,
    skill_name: str,
    questions: list[Any],
    action_label: str,
) -> str:
    summaries = "\n".join(
        f"  {i + 1}. {q.get('question', '')[:200]}" for i, q in enumerate(questions) if isinstance(q, dict)
    )
    return f"已为 Skill '{skill_name}' {action_label} {len(questions)} 条待审批内容：\n{summaries}"

def _generated_metric_id_key(skill_name: str, entry_id: str) -> str:
    return f"{skill_name}\0{entry_id}"

def _strip_skill_md_fence(text: str) -> str:
    """Remove optional markdown code fences around a SKILL.md body."""
    content = str(text or "").strip()
    if not content.startswith("```"):
        return content
    lines = content.split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()

async def _push_auto_rebuild_published_metric(
    job: AutoRebuildJob,
    *,
    version: str,
    request_id: str | None = None,
) -> None:
    """Best-effort AOM ``published`` push after auto rebuild SemVer bump."""
    if not job.push_metrics:
        logger.info(
            "[JiuWenSwarmDeepAdapter] skip published metrics push: "
            "skill=%s version=%s source=%s reason=source_not_auto_watcher",
            job.skill_name,
            version,
            job.source,
        )
        return
    session_id = str(job.session_id or "").strip()
    channel_id = str(job.channel_id or "").strip() or None
    rid = str(
        request_id or job.request_id or "auto-rebuild"
    ).strip() or "auto-rebuild"
    if not session_id:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] skip published metrics push: "
            "skill=%s version=%s request_id=%s reason=no_session_context",
            job.skill_name,
            version,
            rid,
        )
        return
    try:
        from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

        push_context = EvolutionPushContext(
            transport=WebSocketGatewayPushTransport(),
            channel_id=channel_id,
            session_id=session_id,
        )
        await push_evolution_published(
            push_context,
            request_id=rid,
            skill_name=job.skill_name,
            version=version,
            build_push_message=build_server_push_message,
            source="auto",
        )
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] auto published metrics push failed: "
            "skill=%s version=%s request_id=%s error=%s",
            job.skill_name,
            version,
            rid,
            exc,
        )

def _followup_response(action: str, followup_prompt: str, skill_name: str) -> dict[str, Any]:
    return {
        "action": action,
        "followup_prompt": followup_prompt,
        "skill_name": skill_name,
        "result_type": "followup",
    }

def _extract_followup_prompt(slash_result: dict[str, Any] | None) -> str | None:
    """Return follow-up prompt when a slash command should continue as an agent turn."""
    if not isinstance(slash_result, dict):
        return None
    if slash_result.get("result_type") != "followup":
        return None
    prompt = slash_result.get("followup_prompt")
    if not isinstance(prompt, str):
        return None
    prompt = prompt.strip()
    return prompt or None

def _goal_record_payload(record: Any | None) -> dict[str, Any] | None:
    if record is None:
        return None
    to_dict = getattr(record, "to_dict", None)
    return to_dict() if callable(to_dict) else None

def _format_goal_control_message(action: str, goal: dict[str, Any] | None) -> str:
    if action == "get":
        return "No goal in this session." if goal is None else f"Goal: {goal.get('objective', '')}"
    if action == "pause":
        return "Goal paused." if goal is not None else "No goal in this session."
    if action == "clear":
        return "Goal cleared." if goal is None else "Goal was not cleared."
    if action == "resume":
        return "Goal resumed." if goal is not None else "No goal in this session."
    return "Goal set." if goal is not None else "Goal was not set."

def _record_goal_set_history_if_needed(
    request: AgentRequest,
    *,
    action: str | None,
    result_type: str | None,
    goal_payload: dict[str, Any] | None,
) -> None:
    """Write objective as a user history turn only after a successful set."""
    if str(action or "").strip().lower() != "set":
        return
    if result_type in {"goal_error", "goal_confirm_required", None}:
        return
    if not isinstance(goal_payload, dict):
        return
    objective = str(goal_payload.get("objective") or "").strip()
    if not objective:
        return
    params = request.params if isinstance(request.params, dict) else {}
    goal_id = str(goal_payload.get("goal_id") or "").strip() or None
    append_history_record(
        session_id=request.session_id or "default",
        request_id=request.request_id,
        channel_id=request.channel_id,
        role="user",
        content=objective,
        timestamp=time.time(),
        channel_metadata=request.metadata,
        mode=params.get("mode", "unknown"),
        extra={
            "goal_id": goal_id,
            "is_goal_objective_message": True,
        },
    )

def _goal_completed_history_exists(session_id: str, goal_id: str) -> bool:
    """Return True when this session already persisted a completion card for goal_id."""
    message_id = f"goal-completed-{goal_id}"
    try:
        for rec in load_history_records(session_id):
            if not isinstance(rec, dict):
                continue
            if str(rec.get("id") or "") == message_id:
                return True
            if rec.get("is_goal_completed_message") and str(rec.get("goal_id") or "") == goal_id:
                return True
    except Exception:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] goal completed history lookup failed: session_id=%s goal_id=%s",
            session_id,
            goal_id,
            exc_info=True,
        )
    return False

def _record_goal_completed_history_if_needed(
    *,
    session_id: str,
    channel_id: str,
    channel_metadata: dict[str, Any] | None,
    mode: str | None,
    goal_payload: dict[str, Any] | None,
) -> None:
    """Persist a goal-completed card once when status first becomes completed."""
    if not isinstance(goal_payload, dict):
        return
    status = goal_payload.get("status")
    status_value = getattr(status, "value", status)
    if str(status_value or "").strip().lower() != "completed":
        return
    goal_id = str(goal_payload.get("goal_id") or "").strip()
    if not goal_id:
        return
    sid = (session_id or "default").strip() or "default"
    if (sid, goal_id) in _GOAL_COMPLETED_PERSISTED:
        return
    if _goal_completed_history_exists(sid, goal_id):
        return

    evidence = ""
    last_assessment = goal_payload.get("last_assessment")
    if isinstance(last_assessment, dict):
        evidence = str(last_assessment.get("evidence") or "").strip()
    # Keep the existing frontend GoalCompletedCard wire format so history
    # restore / localStorage merge keep working without a content-parser fork.
    content = "goal.completed:" + json.dumps({"evidence": evidence}, ensure_ascii=False)
    message_id = f"goal-completed-{goal_id}"
    append_history_record(
        session_id=sid,
        request_id=message_id,
        channel_id=channel_id,
        role="assistant",
        content=content,
        timestamp=time.time(),
        channel_metadata=channel_metadata,
        mode=mode,
        extra={
            "id": message_id,
            "goal_id": goal_id,
            "is_goal_completed_message": True,
            "evidence": evidence,
        },
    )
    _GOAL_COMPLETED_PERSISTED.add((sid, goal_id))

def _interaction_goal_updated_payload(payload: Any) -> dict[str, Any]:
    """Normalize goal updates to the public Web/TUI payload shape."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import GOAL_UPDATED_EVENT_TYPE
    if not isinstance(payload, dict):
        return {"event_type": GOAL_UPDATED_EVENT_TYPE, "goal": None}
    if "goal" in payload:
        return {
            "event_type": GOAL_UPDATED_EVENT_TYPE,
            "goal": payload.get("goal"),
        }
    return {
        "event_type": GOAL_UPDATED_EVENT_TYPE,
        "goal": payload or None,
    }

def _valid_deepresearch_rewrite_request_id(request_id: object) -> bool:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _DEEPRESEARCH_REWRITE_REQUEST_ID_MAX_LENGTH
    return (
        isinstance(request_id, str)
        and 0 < len(request_id) <= _DEEPRESEARCH_REWRITE_REQUEST_ID_MAX_LENGTH
    )

def _deepresearch_rewrite_replay_conflict() -> RewriteFastPathResult:
    return RewriteFastPathResult(
        recognized=True,
        status="error",
        action=None,
        error_code="REPLAY_CONFLICT",
        message="无法安全重放改写请求，请重新提交。",
        usage_metadata=None,
        prepare_ms=0.0,
        model_ms=0.0,
        commit_ms=0.0,
        total_ms=0.0,
        model_calls=0,
        commit_result=None,
    )

def _deepresearch_rewrite_publish_uncertain(
    target: RewriteHtmlTarget | None = None,
) -> RewriteFastPathResult:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE
    return RewriteFastPathResult(
        recognized=True,
        status="error",
        action=None,
        error_code="PUBLISH_STATE_UNCERTAIN",
        message=_DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE,
        usage_metadata=None,
        prepare_ms=0.0,
        model_ms=0.0,
        commit_ms=0.0,
        total_ms=0.0,
        model_calls=0,
        commit_result=(
            {
                "status": "completed",
                "report_delivered": False,
                "report_path": target.report_path,
                "revision_id": target.revision_id,
            }
            if target is not None
            else None
        ),
    )

def _deepresearch_rewrite_terminal_kind(
    result: RewriteFastPathResult,
) -> str | None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE
    commit_result = result.commit_result
    target = (
        target_from_commit_result(commit_result)
        if isinstance(commit_result, dict)
        else None
    )
    publish_uncertain = (
        result.status == "error"
        and result.action is None
        and result.error_code == "PUBLISH_STATE_UNCERTAIN"
        and result.message == _DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE
        and isinstance(commit_result, dict)
        and commit_result.get("status") == "completed"
        and commit_result.get("report_delivered") is False
        and target is not None
    )
    if publish_uncertain:
        return "publish_uncertain"
    invalid_completed_result = (
        result.status != "completed"
        or not isinstance(commit_result, dict)
        or commit_result.get("status") != "completed"
        or not isinstance(commit_result.get("report_delivered"), bool)
        or target is None
    )
    if invalid_completed_result:
        return None
    if commit_result["report_delivered"] is False:
        return "report_delivery_failed"
    if result.error_code is None:
        return "success"
    return None

def _normalize_deepresearch_usage(value: object) -> dict[str, float] | None:
    keys = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "input_cost",
        "output_cost",
        "total_cost",
    )
    if isinstance(value, dict):
        raw = value
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                raw = model_dump()
            except Exception:  # noqa: BLE001
                raw = {}
        else:
            raw = {key: getattr(value, key, None) for key in keys}
    if not isinstance(raw, dict):
        return None
    normalized = {}
    for key in keys:
        item = raw.get(key)
        if isinstance(item, (int, float)) and not isinstance(raw.get(key), bool):
            normalized[key] = operator.getitem(raw, key)
    return normalized or None

def _deepresearch_usage_summary(
    usage: dict[str, float],
) -> dict[str, float | str]:
    summary: dict[str, float | str] = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    input_tokens = usage.get("input_tokens", 0)
    cache_tokens = usage.get("cache_tokens", 0)
    if input_tokens and cache_tokens:
        summary["cache_tokens"] = cache_tokens
        summary["cache_hit_rate"] = f"{cache_tokens / input_tokens:.1%}"
    for cost in ("input_cost", "output_cost", "total_cost"):
        if usage.get(cost, 0) > 0:
            summary[cost] = round(usage[cost], 6)
    return summary

def _is_approval_event(evt) -> bool:
    """Check whether an OutputSchema event is an approval request."""
    evt_type = getattr(evt, "type", "")
    if evt_type == "chat.ask_user_question":
        return True
    if hasattr(evt, "payload") and isinstance(evt.payload, dict):
        return evt.payload.get("event_type") == "chat.ask_user_question"
    return False
