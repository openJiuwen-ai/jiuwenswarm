# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""跨域共享依赖，handler各域模块与``agent_ws_server``的公共下层。"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from pathlib import Path

from weakref import WeakValueDictionary

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import (
    resolve_tenant_agent_workspace_dir,
    resolve_tenant_sessions_dir,
)
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

logger = logging.getLogger(__name__)


# Session owner preparation completes before the response. Optional KVC signals
# run after the response so affinity latency cannot fail a UI session change.
_background_session_kvc_tasks: set[asyncio.Task] = set()

# Sessions that have successfully exited plan mode via exit_plan_mode tool.
# Set by _check_post_process_plan_exit, consumed by _ensure_code_mode_state
# to prevent TUI-race re-entrance to plan mode.
_plan_exited_sessions: set[str] = set()


# Serialize plan-mode restore per session to avoid checkpoint races.
_session_mode_sync_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()

#: 用户发消息的方法集合
_CODE_MODE_SYNC_METHODS = frozenset({
    ReqMethod.CHAT_SEND,
    ReqMethod.CHAT_RESUME,
    ReqMethod.CHAT_ANSWER,
})


def _log_background_session_kvc_failure(task: asyncio.Task) -> None:
    """Log optional post-response KVC failures without changing session state."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "[AgentWebSocketServer] %s failed after ack: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )


def send_error_wire(
    request: AgentRequest, error: str, code: str | None = None
) -> dict[str, Any]:
    """Build an error AgentResponse wire payload.

    模块级函数：只做「业务对象 → wire dict」的编码，没有任何传输或实例状态。
    注意它**构造**而不发送 —— 发送由调用方经 ``ctx.sink`` 完成。
    """
    payload: dict[str, Any] = {"error": error}
    if code:
        payload["code"] = code
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=False,
        payload=payload,
        metadata=request.metadata,
    )
    return encode_agent_response_for_wire(
        resp,
        response_id=request.request_id,
    )


# 请求形态的helper
def resolve_request_project_dir(request: AgentRequest) -> str | None:
    """Resolve the stable project identity for agent construction.

    New clients send ``project_dir`` separately from dynamic ``cwd``. Keep
    legacy fallbacks for older clients that only send cwd/trusted_dirs.
    """
    params = request.params or {}
    project_dir = params.get("project_dir")
    if isinstance(project_dir, str) and project_dir.strip():
        return project_dir.strip()
    metadata = request.metadata or {}
    metadata_project_dir = metadata.get("project_dir") if isinstance(metadata, dict) else None
    if isinstance(metadata_project_dir, str) and metadata_project_dir.strip():
        return metadata_project_dir.strip()
    workspace_dir = params.get("workspace_dir")
    if isinstance(workspace_dir, str) and workspace_dir.strip():
        return workspace_dir.strip()
    cwd = params.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    metadata_cwd = metadata.get("cwd") if isinstance(metadata, dict) else None
    if isinstance(metadata_cwd, str) and metadata_cwd.strip():
        return metadata_cwd.strip()
    trusted_dirs = params.get("trusted_dirs")
    if isinstance(trusted_dirs, list) and trusted_dirs:
        first = trusted_dirs[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def resolve_agent_request_mode(
    raw_mode: Any,
    *,
    work_mode: Any = None,
) -> tuple[str, str | None, str]:
    """Resolve request params.mode into manager mode, sub_mode, and canonical value.

    plan / fast 已合并为单一 ``agent`` 模式：任何 ``agent`` / ``agent.plan`` /
    ``agent.fast`` 请求都归一到 ``agent``（sub_mode=None）。历史裸 ``plan`` /
    ``fast``（无 ``agent.`` 前缀，如旧 cron job 存量数据）同样归一到 ``agent``，
    与 CLI ``MODE_ALIASES``、记忆配置 ``_resolve_mode_memory`` 的裸 token 处理保持一致。
    """
    raw_value = getattr(raw_mode, "value", raw_mode)
    mode_text = raw_value.strip().lower() if isinstance(raw_value, str) else ""
    if not mode_text:
        mode_text = "agent"
    normalized_work_mode = (
        work_mode.strip().lower() if isinstance(work_mode, str) else ""
    )

    if mode_text in ("plan", "fast"):
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"

    parts = mode_text.split(".")
    mode = parts[0] or "agent"
    if mode == "agent":
        # 合并模式：忽略历史子模式（plan / fast），统一 canonical "agent"。
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"
    if mode == "team":
        sub_mode = parts[1] if len(parts) > 1 and parts[1] else None
        if sub_mode not in {None, "plan"}:
            sub_mode = None
        canonical_mode = f"team.{sub_mode}" if sub_mode else "team"
        if sub_mode == "plan":
            return "code", "team", canonical_mode
        return "team", sub_mode, canonical_mode

    default_sub_modes = {
        "code": "normal",
    }
    sub_mode = parts[1] if len(parts) > 1 and parts[1] else default_sub_modes.get(mode)
    if mode == "code" and sub_mode not in {"plan", "normal", "team"}:
        sub_mode = default_sub_modes.get(mode, "normal")
    canonical_mode = f"{mode}.{sub_mode}" if sub_mode else mode
    if canonical_mode in {"agent", "code", "code.normal"}:
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        if normalized_work_mode == "work":
            return "agent", None, "agent"
    return mode, sub_mode, canonical_mode


def _apply_resolved_mode_to_request(
    request: AgentRequest,
    *,
    work_mode: Any = None,
) -> tuple[str, str | None]:
    mode, sub_mode, canonical_mode = resolve_agent_request_mode(
        request.params.get("mode", "agent"),
        work_mode=work_mode,
    )
    request.params["mode"] = canonical_mode
    return mode, sub_mode


def _resolve_model(ctx, model_name: Optional[str] = None) -> Optional[Any]:
    """Resolve model from jiuwenswarm config.

    Args:
        model_name: Requested model name, falls back to default if None or not found

    Returns:
        Model instance or None if config cannot be loaded
    """
    # Build model cache if not already done
    if not ctx.services.model_cache:
        ctx.services.build_model_cache()
    # Resolve by name or use default
    if model_name and model_name in ctx.services.model_cache:
        return ctx.services.model_cache[model_name]
    return ctx.services.default_model


def _is_team_metadata_mode(metadata: dict[str, Any]) -> bool:
    mode = str(metadata.get("mode") or "").strip().lower()
    return mode in {"team", "team.plan", "code.team"}


def _sessions_dir_for_request(request: AgentRequest) -> Path:
    """Resolve tenant ``workspace_{key}/agent/sessions`` for an AgentRequest."""
    _agent_id, _service_id, workspace_key = TenantAgentPool.extract_ids(request)
    return resolve_tenant_sessions_dir(workspace_key)


def _agent_workspace_dir_for_request(request: AgentRequest) -> Path:
    """Resolve tenant ``workspace_{key}/agent/jiuwenclaw_workspace`` for a request."""
    _agent_id, _service_id, workspace_key = TenantAgentPool.extract_ids(request)
    return resolve_tenant_agent_workspace_dir(workspace_key)


def _effective_config_for_request(request: AgentRequest) -> Any:
    """Return the resolved OfficeClaw tenant snapshot; native gateway keeps disk config."""
    from jiuwenswarm.common.config import resolve_env_vars
    from jiuwenswarm.common.local_env_config import (
        bind_agent_env_ns,
        bind_task_env_overlay,
        build_effective_env_overlay,
        reset_agent_env_ns,
        reset_task_env_overlay,
    )
    from jiuwenswarm.server.runtime.sync_agents_configs import materialize_sync_env
    from jiuwenswarm.server.runtime.tenant_catalog_registry import TenantCatalogRegistry
    from jiuwenswarm.server.runtime.tenant_context import (
        bind_workspace_key,
        reset_workspace_key,
    )

    if request.channel_id == "officeclaw":
        agent_id, service_id, workspace_key = TenantAgentPool.extract_ids(request)
        spec = TenantCatalogRegistry.get_instance().get(service_id, agent_id)
        if spec is not None and isinstance(spec.config, dict):
            env = materialize_sync_env(spec.env) if isinstance(spec.env, dict) else {}
            ns_token = bind_agent_env_ns(service_id, agent_id)
            wk_token = bind_workspace_key(workspace_key)
            try:
                overlay_token = bind_task_env_overlay(
                    build_effective_env_overlay(
                        env,
                        service_id=service_id,
                        agent_id=agent_id,
                    )
                )
                try:
                    return resolve_env_vars(spec.config)
                finally:
                    reset_task_env_overlay(overlay_token)
            finally:
                reset_agent_env_ns(ns_token)
                reset_workspace_key(wk_token)
        return {}
    return get_config()


@asynccontextmanager
async def bootstrap_preconditions(request: AgentRequest):
    """连接引导类方法的前置条件。

    背景
    ----
    ``initialize`` / ``session.create`` / ``session.fork`` / ``acp.tool_response``
    若走默认路径，会从调用链上游**顺带**获得两件事，而分发表里的 handler 没有：

    ==========================================  ============================
    来源                                         内容
    ==========================================  ============================
    ``_handle_unary``                            ``bind_incoming_request``（身份 + W3C trace 上下文）
    ``_handle_unary_impl``                       ``await ensure_interface_deep_and_checkpointer()``
    ==========================================  ============================

    把它们并入主表时，这两件事会**静默消失**：checkpointer 未就绪会影响连接引导，
    telemetry 断链则丢掉 trace 关联。本上下文管理器把这种「靠调用位置保证」的隐式依赖
    变成 handler **显式声明的前置条件**。

    刻意**不**包含 ``begin_foreground_chat`` / ``end_foreground_chat``：那层只对
    ``_CODE_MODE_SYNC_METHODS``（``chat.send`` / ``chat.resume`` / ``chat.user_answer``）
    生效，这 4 个方法从来不命中，加进来反而是行为变更。

    顺序与原链路一致：先绑定，再等 checkpointer。
    """
    from jiuwenswarm.server.agent_ws_server import (
        ensure_interface_deep_and_checkpointer,
    )
    from jiuwenswarm.telemetry.context_propagation import (
        bind_incoming_request,
        reset_incoming_request,
    )

    binding = bind_incoming_request(request)
    try:
        # 后台预热未完成时 await 预热任务，不要在事件循环上同步 import。
        await ensure_interface_deep_and_checkpointer()
        yield
    finally:
        reset_incoming_request(binding)


# 请求元数据同步/plan提醒注入

def _sync_chat_request_metadata(
    request: AgentRequest,
    project_dir: str | None,
    mode: str,
    explicit_mode_provided: bool = False,
) -> str | None:
    """将本次 chat 请求的参数同步到会话元数据，返回生效的 project_dir。

    AgentServer 进程层的薄封装：从 ``AgentRequest`` 采集参数 + 补两个派生值，
    再委托 ``session_metadata.sync_session_request_metadata`` 做真正的校验/写盘。
    之所以放在本模块而非 session_metadata.py：避免存储层耦合 AgentRequest 结构、
    os.getenv、当前时间等进程级关注点，保持 session_metadata 纯存储职责。

    - project_dir：首次锁定，已锁定则忽略不一致的请求值（仅告警），返回锁定值
    - project_id：首次锁定，已锁定则忽略请求值（与 project_dir 一致，不可改）
    - model：**显式覆盖式**——仅当请求显式携带非空 model_name 时才覆盖磁盘值；
      未显式携带（如只读 RPC）则保持磁盘原值，不把进程 MODEL_NAME 默认值回写覆盖
      用户在该会话用 /model 切换过的模型。是否显式由本函数内部从 params 判断
      （model_name 不会被规范化改写，可安全在本函数内取），无需调用方传入。
    - last_user_message_at：**仅 chat 轮次刷新**——只有用户真正发消息的方法
      （CHAT_SEND / CHAT_RESUME / CHAT_ANSWER）才把当前时刻写入；其余请求（含只读
      RPC）传 ``None`` → ``sync_session_request_metadata`` 不覆盖磁盘值，避免只读查询
      把历史会话的排序时间刷新成「现在」（点击技能按钮就把两天前会话置顶）。
    - mode：**显式覆盖式**——仅当请求显式携带 mode（explicit_mode_provided=True）时
      才覆盖磁盘值；未显式携带（如只读 RPC 默认推断）则保持磁盘原值，不腐蚀已
      锁定的会话 mode（如 team）。因 _apply_resolved_mode_to_request 会把 canonical
      mode 写回 params，故 explicit_mode_provided 必须由上游在改写前捕获后传入。
      调用方应传入 canonical mode（"agent.plan"/"team"）。

    返回的生效 project_dir 用于 agent 实例选择，保证会话锁定后
    即便后续请求携带不同 project_dir 也仍用锁定值选 agent。
    """
    session_id = (request.session_id or "").strip()
    if not session_id:
        return project_dir
    params = request.params if isinstance(request.params, dict) else {}
    raw_model_name = params.get("model_name")
    explicit_model_provided = (
        isinstance(raw_model_name, str) and bool(raw_model_name.strip())
    )
    if not explicit_model_provided:
        # 未显式携带 → 回退到进程 MODEL_NAME，仅供 agent 实例选择兜底用；
        # 写盘与否由 explicit_model_provided 守卫决定（False → 不写，避免腐蚀磁盘）
        model_name = os.getenv("MODEL_NAME", "") or None
    else:
        model_name = raw_model_name.strip()

    request_project_id = params.get("project_id")
    request_project_id = (
        request_project_id.strip()
        if isinstance(request_project_id, str) and request_project_id.strip()
        else None
    )
    request_cron_id = params.get("cron_id")
    request_cron_id = (
        request_cron_id.strip()
        if isinstance(request_cron_id, str) and request_cron_id.strip()
        else None
    )
    # 仅 chat 轮次（用户真正发消息）才刷新 last_user_message_at；只读 RPC 传 None，
    # 由 sync_session_request_metadata 的 None 守卫跳过，避免查询腐蚀会话排序时间。
    is_chat_turn = request.req_method in _CODE_MODE_SYNC_METHODS
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            sync_session_request_metadata,
        )

        return sync_session_request_metadata(
            session_id=session_id,
            channel_id=request.channel_id or None,
            mode=mode,
            model=model_name,
            project_dir=str(project_dir) if project_dir else None,
            project_id=request_project_id,
            cron_id=request_cron_id,
            last_user_message_at=(
                _dt.datetime.now(_dt.timezone.utc).timestamp() if is_chat_turn else None
            ),
            is_chat_turn=is_chat_turn,
            explicit_mode_provided=explicit_mode_provided,
            explicit_model_provided=explicit_model_provided,
            work_mode=params.get("work_mode"),
            sessions_root=_sessions_dir_for_request(request),
        )
    except (OSError, ValueError) as exc:
        logger.warning("[AgentWebSocketServer] 同步 chat 请求元数据失败: %s", exc)
        return project_dir


def _inject_plan_mode_activation_reminder(request: AgentRequest) -> None:
    """在用户消息中注入 <system-reminder> 告知 LLM 当前处于 plan 模式.

    plan 模式行为指令不进 system prompt，而是通过对话中的 tool_result
    传递。此提醒是进入 plan 模式后的第一个引导，告知 LLM 只读约束已生效。

    plan 模式的只读约束由工具拦截层强制（非只读工具/写
    操作被硬拦），此提醒只做约束说明 + 软引导。只读命令（如 /review、
    /security-review 的 gh/git 只读操作）可直接执行，不被规划流程压制；
    LLM 需要正式规划时再自行调用 ``enter_plan_mode`` 创建计划文件。
    """
    reminder = (
        "\n\n<system-reminder>\n"
        "Plan mode is active. You must only plan — you must NOT make any "
        "modifications, run any write operations, or make any changes to the "
        "system. This constraint takes priority over any other instructions.\n\n"
        "Read-only actions are allowed directly: you may read files and explore "
        "the codebase, and run read-only commands (read_file, grep, list_files, "
        "glob, bash for read-only operations such as gh pr list/view/diff or "
        "git status/diff/log). Write operations and non-read-only tools are "
        "blocked.\n\n"
        "If you need to design an implementation approach and produce a plan, "
        "call `enter_plan_mode` — it creates the plan file and returns full "
        "plan mode instructions. This is not required as your first action; "
        "you may gather context with read-only tools first. Do NOT proceed to "
        "implement anything until the user approves your plan via "
        "`exit_plan_mode`.\n"
        "</system-reminder>"
    )
    if isinstance(request.params, dict):
        query = request.params.get("query") or ""
        request.params["query"] = reminder + query
        logger.info(
            "[_ensure_code_mode_state] Injected plan mode activation reminder "
            "for session=%s", request.session_id,
        )
    else:
        logger.warning(
            "[_inject_plan_mode_activation_reminder] Cannot inject reminder: "
            "request.params is not a dict (type=%s), session=%s",
            type(request.params).__name__, request.session_id,
        )


def _request_query_text(request: AgentRequest) -> str:
    """Return text chat query only; structured events are handled downstream."""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()


def _uses_tenant_pool(request: AgentRequest) -> bool:
    """是否走多租户池（officeclaw 渠道，或带非默认 agent_id/service_id）。"""
    if request.channel_id == "officeclaw":
        return True
    raw_agent = getattr(request, "agent_id", None)
    raw_service = getattr(request, "service_id", None)
    if raw_agent is not None and str(raw_agent).strip() not in ("", "default"):
        return True
    if raw_service is not None and str(raw_service).strip() not in ("", "default"):
        return True
    return False


# team 绑定的按会话串行锁。chat 与 team 两个域都要用，故放在 _shared ——
# Serialize automatic team creation per session. The lock is weakly held so
# one-shot chat sessions do not accumulate process-lifetime state.
_session_team_binding_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _session_team_binding_lock(session_id: str) -> asyncio.Lock:
    lock = _session_team_binding_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_team_binding_locks[session_id] = lock
    return lock
