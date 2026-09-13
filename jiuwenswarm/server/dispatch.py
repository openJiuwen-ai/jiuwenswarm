# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""请求分发注册表"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
    get_permissions_config_req_methods,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import agents as agents_handlers
from jiuwenswarm.server.handlers import bootstrap as bootstrap_handlers
from jiuwenswarm.server.handlers import chat as chat_handlers
from jiuwenswarm.server.handlers import commands as commands_handlers
from jiuwenswarm.server.handlers import extensions as extensions_handlers
from jiuwenswarm.server.handlers import file_transfer as file_transfer_handlers
from jiuwenswarm.server.handlers import mcp as mcp_handlers
from jiuwenswarm.server.handlers import ops as ops_handlers
from jiuwenswarm.server.handlers import permissions as permissions_handlers
from jiuwenswarm.server.handlers import sandbox as sandbox_handlers
from jiuwenswarm.server.handlers import schedule as schedule_handlers
from jiuwenswarm.server.handlers import session as session_handlers
from jiuwenswarm.server.handlers import team as team_handlers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandlerSpec:
    """一条分发规则：指向一个**传输无关的自由函数**。

    Attributes:
        fn: 处理函数，签名 ``async def (ctx: RequestContext, *args, **kwargs)``。
        args: 追加的位置参数（如 ``handle_schedule_request`` 的 action）。
        kwargs: 追加的关键字参数（如 ``restore_files=True``）。
        stream_fn: ``is_stream`` 为真时改用的函数（目前仅 ``history.get`` 用到）。
    """

    fn: Any = None
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    stream_fn: Any = None

    def __post_init__(self) -> None:
        if self.fn is None:
            raise ValueError("HandlerSpec 必须指定 fn（传输无关的自由函数）")
        if not callable(self.fn):
            raise ValueError(f"HandlerSpec.fn 不可调用: {self.fn!r}")
        if self.stream_fn is not None and not callable(self.stream_fn):
            raise ValueError(f"HandlerSpec.stream_fn 不可调用: {self.stream_fn!r}")

    def resolve_fn(self, is_stream: bool) -> Any:
        """解析实际调用的函数。"""
        if self.stream_fn and is_stream:
            return self.stream_fn
        return self.fn


def _schedule(action: str) -> HandlerSpec:
    """``schedule.*`` / ``issue.*`` 共用一个 handler，按 action 分派。"""
    return HandlerSpec(fn=schedule_handlers.handle_schedule_request, args=(action,))


HANDLERS: dict[ReqMethod, HandlerSpec] = {
    # --- 连接引导 ---
    # 这几个方法走默认路径时会顺带获得 telemetry 绑定与 checkpointer 等待；
    # 进表后改由 handlers/bootstrap.py 的 bootstrap_preconditions 显式提供，
    # 见该模块说明。
    ReqMethod.INITIALIZE: HandlerSpec(fn=bootstrap_handlers.handle_initialize),
    ReqMethod.SESSION_CREATE: HandlerSpec(fn=bootstrap_handlers.handle_session_create),
    ReqMethod.SESSION_FORK: HandlerSpec(fn=bootstrap_handlers.handle_session_fork),
    ReqMethod.ACP_TOOL_RESPONSE: HandlerSpec(fn=bootstrap_handlers.handle_acp_tool_response),
    # --- 会话 ---
    # 整域实现在 handlers/session.py。
    ReqMethod.SESSION_LIST: HandlerSpec(fn=session_handlers.handle_session_list),
    ReqMethod.SESSION_RENAME: HandlerSpec(fn=session_handlers.handle_session_rename),
    ReqMethod.SESSION_SWITCH: HandlerSpec(fn=session_handlers.handle_session_switch),
    ReqMethod.SESSION_DELETE: HandlerSpec(fn=session_handlers.handle_session_delete),
    ReqMethod.SESSION_REWIND: HandlerSpec(fn=session_handlers.handle_session_rewind_full),
    ReqMethod.SESSION_REWIND_AND_RESTORE: HandlerSpec(
        fn=session_handlers.handle_session_rewind_full, kwargs={"restore_files": True}
    ),
    ReqMethod.SESSION_REWIND_COMPACT: HandlerSpec(
        fn=session_handlers.handle_session_rewind_full, kwargs={"compact": True}
    ),
    ReqMethod.SESSION_REWIND_CONTEXT: HandlerSpec(fn=session_handlers.handle_session_rewind_context),
    # history.get：流式/非流式走不同 handler。
    ReqMethod.HISTORY_GET: HandlerSpec(
        fn=session_handlers.handle_history_get,
        stream_fn=session_handlers.handle_history_get_stream,
    ),
    # --- 中断 ---
    # 实现在 handlers/chat.py。chat.send / chat.resume 走默认路径，不在表内。
    ReqMethod.CHAT_CANCEL: HandlerSpec(fn=chat_handlers.handle_chat_cancel_dispatch),
    # Gateway ↔ Agent 分块文件传输（默认跟随 is_enterprise；YAML 可显式覆盖）
    ReqMethod.FILE_TRANSFER_START: HandlerSpec(fn=file_transfer_handlers.handle_file_transfer),
    ReqMethod.FILE_TRANSFER_CHUNK: HandlerSpec(fn=file_transfer_handlers.handle_file_transfer),
    ReqMethod.FILE_TRANSFER_COMPLETE: HandlerSpec(fn=file_transfer_handlers.handle_file_transfer),
    # --- 团队 ---
    # 整域实现在 handlers/team.py。
    ReqMethod.TEAM_TEMPLATES_LIST: HandlerSpec(fn=team_handlers.handle_team_templates_list),
    ReqMethod.TEAM_BINDINGS_LIST: HandlerSpec(fn=team_handlers.handle_team_bindings_list),
    ReqMethod.TEAM_BINDING_CREATE: HandlerSpec(fn=team_handlers.handle_team_binding_create),
    ReqMethod.TEAM_BINDING_GENERATE: HandlerSpec(fn=team_handlers.handle_team_binding_generate),
    ReqMethod.TEAM_SESSION_BIND: HandlerSpec(fn=team_handlers.handle_team_session_bind),
    ReqMethod.TEAM_DELETE: HandlerSpec(fn=team_handlers.handle_team_delete),
    ReqMethod.TEAM_SESSION_RESET: HandlerSpec(fn=team_handlers.handle_team_session_reset),
    ReqMethod.TEAM_RUNTIME_DISSOLVE: HandlerSpec(fn=team_handlers.handle_team_runtime_dissolve),
    ReqMethod.TEAM_SNAPSHOT: HandlerSpec(fn=team_handlers.handle_team_snapshot),
    ReqMethod.TEAM_MQ_PUBLISH: HandlerSpec(fn=team_handlers.handle_team_mq_publish),
    ReqMethod.TEAM_HISTORY_GET: HandlerSpec(fn=team_handlers.handle_team_history_get),
    ReqMethod.TEAM_MEMBERS_GET: HandlerSpec(fn=team_handlers.handle_team_members_get),
    ReqMethod.TEAM_TASKS_DEPENDENCIES: HandlerSpec(fn=team_handlers.handle_team_tasks_dependencies),
    # --- 命令 ---
    # 整域实现在 handlers/commands.py。
    ReqMethod.COMMAND_WORKFLOWS: HandlerSpec(fn=commands_handlers.handle_command_workflows),
    ReqMethod.COMMAND_ADD_DIR: HandlerSpec(fn=commands_handlers.handle_command_add_dir),
    ReqMethod.COMMAND_CHROME: HandlerSpec(fn=commands_handlers.handle_command_chrome),
    ReqMethod.COMMAND_COMPACT: HandlerSpec(fn=commands_handlers.handle_command_compact),
    ReqMethod.COMMAND_COMPACT_PARTIAL: HandlerSpec(fn=commands_handlers.handle_command_compact_partial),
    ReqMethod.COMMAND_CONTEXT: HandlerSpec(fn=commands_handlers.handle_command_context),
    ReqMethod.COMMAND_RECAP: HandlerSpec(fn=commands_handlers.handle_command_recap),
    ReqMethod.COMMAND_BTW: HandlerSpec(fn=commands_handlers.handle_command_btw),
    ReqMethod.COMMAND_DIFF: HandlerSpec(fn=commands_handlers.handle_command_diff),
    ReqMethod.COMMAND_SIMPLIFY: HandlerSpec(fn=commands_handlers.handle_command_simplify),
    ReqMethod.COMMAND_MODEL: HandlerSpec(fn=commands_handlers.handle_command_model),
    ReqMethod.COMMAND_MCP: HandlerSpec(fn=mcp_handlers.handle_command_mcp),
    ReqMethod.COMMAND_SANDBOX: HandlerSpec(fn=sandbox_handlers.handle_command_sandbox),
    ReqMethod.COMMAND_RESUME: HandlerSpec(fn=commands_handlers.handle_command_resume),
    ReqMethod.COMMAND_SESSION: HandlerSpec(fn=commands_handlers.handle_command_session),
    ReqMethod.COMMAND_STATUS: HandlerSpec(fn=commands_handlers.handle_command_status),
    # --- 智能体 ---
    # 整域实现在 handlers/agents.py。
    ReqMethod.AGENTS_LIST: HandlerSpec(fn=agents_handlers.handle_agents_list),
    ReqMethod.AGENTS_GET: HandlerSpec(fn=agents_handlers.handle_agents_get),
    ReqMethod.AGENTS_CREATE: HandlerSpec(fn=agents_handlers.handle_agents_create),
    ReqMethod.AGENTS_UPDATE: HandlerSpec(fn=agents_handlers.handle_agents_update),
    ReqMethod.AGENTS_DELETE: HandlerSpec(fn=agents_handlers.handle_agents_delete),
    ReqMethod.AGENTS_ENABLE: HandlerSpec(fn=agents_handlers.handle_agents_set_enabled, args=(True,)),
    ReqMethod.AGENTS_DISABLE: HandlerSpec(fn=agents_handlers.handle_agents_set_enabled, args=(False,)),
    ReqMethod.AGENTS_TOOLS_LIST: HandlerSpec(fn=agents_handlers.handle_agents_tools_list),
    # --- 扩展 / Hooks / Harness 包 ---
    # 整域实现在 handlers/extensions.py。
    ReqMethod.EXTENSIONS_LIST: HandlerSpec(fn=extensions_handlers.handle_extensions_list),
    ReqMethod.EXTENSIONS_IMPORT: HandlerSpec(fn=extensions_handlers.handle_extensions_import),
    ReqMethod.EXTENSIONS_DELETE: HandlerSpec(fn=extensions_handlers.handle_extensions_delete),
    ReqMethod.EXTENSIONS_TOGGLE: HandlerSpec(fn=extensions_handlers.handle_extensions_toggle),
    ReqMethod.HOOKS_LIST: HandlerSpec(fn=extensions_handlers.handle_hooks_list),
    ReqMethod.HARNESS_PACKAGES_GET: HandlerSpec(fn=extensions_handlers.handle_harness_packages_get),
    ReqMethod.HARNESS_PACKAGES_SCAN: HandlerSpec(fn=extensions_handlers.handle_harness_packages_scan),
    ReqMethod.HARNESS_PACKAGES_ACTIVATE: HandlerSpec(fn=extensions_handlers.handle_harness_packages_activate),
    ReqMethod.HARNESS_PACKAGES_DEACTIVATE: HandlerSpec(fn=extensions_handlers.handle_harness_packages_deactivate),
    ReqMethod.HARNESS_PACKAGES_DELETE: HandlerSpec(fn=extensions_handlers.handle_harness_packages_delete),
    # --- 调度 / 议题（共用 _handle_schedule_request，按 action 分派）---
    ReqMethod.SCHEDULE_CHECK_CONFIG: _schedule("check_config"),
    ReqMethod.SCHEDULE_UPDATE_CONFIG: _schedule("update_config"),
    ReqMethod.SCHEDULE_CREATE: _schedule("create"),
    ReqMethod.SCHEDULE_RUN: _schedule("run"),
    ReqMethod.SCHEDULE_LIST: _schedule("list"),
    ReqMethod.SCHEDULE_STATUS: _schedule("status"),
    ReqMethod.SCHEDULE_LOGS: _schedule("logs"),
    ReqMethod.SCHEDULE_CANCEL: _schedule("cancel"),
    ReqMethod.SCHEDULE_DELETE: _schedule("delete"),
    ReqMethod.ISSUE_WATCH_ONCE: _schedule("issue_watch_once"),
    ReqMethod.ISSUE_STATE_LIST: _schedule("issue_state_list"),
    ReqMethod.ISSUE_DELETE: _schedule("issue_delete"),
    ReqMethod.ISSUE_MATRIX: _schedule("issue_matrix"),
    # --- 运维 ---
    # 整域实现在 handlers/ops.py。
    ReqMethod.PROACTIVE_TICK: HandlerSpec(fn=ops_handlers.handle_proactive_tick),
    ReqMethod.BROWSER_RUNTIME_RESTART: HandlerSpec(fn=ops_handlers.handle_browser_runtime_restart),
    ReqMethod.CONFIG_CACHE_CLEAR: HandlerSpec(fn=ops_handlers.handle_config_cache_clear),
    ReqMethod.AGENT_RELOAD_CONFIG: HandlerSpec(fn=ops_handlers.handle_agent_reload_config),
    ReqMethod.SYNC_AGENTS_CONFIGS: HandlerSpec(fn=ops_handlers.handle_sync_agents_configs),
    ReqMethod.AGENT_PREWARM_SYNC: HandlerSpec(fn=ops_handlers.handle_agent_prewarm_sync),
}


def _register_permissions_methods() -> None:
    """``permissions.*`` 是一组方法共用 ``_handle_permissions_config``。"""
    spec = HandlerSpec(fn=permissions_handlers.handle_permissions_config)
    for method in get_permissions_config_req_methods():
        if method in HANDLERS:
            raise RuntimeError(f"duplicate handler registration for {method}")
        HANDLERS[method] = spec


_register_permissions_methods()


def supported_methods() -> frozenset[ReqMethod]:
    """表驱动分发覆盖的方法集合。"""
    return frozenset(HANDLERS)


async def dispatch_with_context(ctx: Any, request: Any) -> bool:
    """按表分发，使用**调用方已构造好的** ctx。"""
    return await dispatch_to_handler(
        None, None, request, None, context_factory=lambda: ctx
    )


async def dispatch_to_handler(
    server: Any,
    ws: Any,
    request: Any,
    send_lock: Any,
    *,
    context_factory: Any = None,
) -> bool:
    """按表分发到传输无关的 handler。

    Args:
        server: ``AgentWebSocketServer`` 实例
        ws: 传输对象，仅用于构造默认 ctx 的 sink
        send_lock: 连接级发送锁
        context_factory: 可选，``() -> RequestContext``

    Returns:
        ``True`` 已由表内 handler 处理；``False`` 未命中，调用方应走默认路径
    """
    spec = HANDLERS.get(request.req_method)
    if spec is None:
        return False

    is_stream = bool(getattr(request, "is_stream", False))
    ctx = context_factory() if context_factory else _default_context(server, ws, send_lock, request)
    await spec.resolve_fn(is_stream)(ctx, *spec.args, **dict(spec.kwargs))
    return True


def _default_context(server: Any, ws: Any, send_lock: Any, request: Any) -> Any:
    """为新式 handler 构造默认（WebSocket）上下文。"""
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    return RequestContext(
        request=request,
        sink=WSSink(ws, send_lock),
        connection_id=str(id(ws)),
        services=AgentServerServices(server),
    )
