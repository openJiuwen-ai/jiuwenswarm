# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebSocket 查表：请求类型 → 业务函数。

只给 WebSocket 用。HTTP 以后按路由直接调用 handlers，不经过这张表。
还没搬出 WebSocket 类的请求不会出现在表里，仍走原来的判断。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers.agents import (
    handle_agents_create,
    handle_agents_delete,
    handle_agents_disable,
    handle_agents_enable,
    handle_agents_get,
    handle_agents_list,
    handle_agents_tools_list,
    handle_agents_update,
)
from jiuwenswarm.server.handlers.commands import (
    handle_command_add_dir,
    handle_command_btw,
    handle_command_chrome,
    handle_command_compact,
    handle_command_compact_partial,
    handle_command_context,
    handle_command_diff,
    handle_command_model,
    handle_command_recap,
    handle_command_resume,
    handle_command_session,
    handle_command_simplify,
    handle_command_status,
    handle_command_workflows,
)
from jiuwenswarm.server.handlers.extensions import (
    handle_extensions_delete,
    handle_extensions_import,
    handle_extensions_list,
    handle_extensions_toggle,
    handle_harness_packages_activate,
    handle_harness_packages_deactivate,
    handle_harness_packages_delete,
    handle_harness_packages_get,
    handle_harness_packages_scan,
    handle_hooks_list,
)
from jiuwenswarm.server.handlers.mcp import handle_command_mcp
from jiuwenswarm.server.handlers.mcp_hub import (
    handle_mcp_cancel_connect,
    handle_mcp_connect,
    handle_mcp_delete_custom,
    handle_mcp_disconnect,
    handle_mcp_install,
    handle_mcp_list,
    handle_mcp_register_custom,
    handle_mcp_save_credentials,
    handle_mcp_show,
    handle_mcp_uninstall,
    handle_mcp_wait_auth,
)
from jiuwenswarm.server.handlers.ops import (
    handle_agent_prewarm_sync,
    handle_agent_reload_config,
    handle_auth_credentials_update,
    handle_browser_runtime_restart,
    handle_config_cache_clear,
    handle_proactive_feedback,
    handle_proactive_tick,
)
from jiuwenswarm.server.handlers.permissions import handle_permissions_config
from jiuwenswarm.server.handlers.sandbox import handle_command_sandbox
from jiuwenswarm.server.handlers.schedule import handle_schedule_request
from jiuwenswarm.server.handlers.session import (
    handle_history_append_record,
    handle_history_get,
    handle_session_input_intent,
    handle_session_list,
    handle_session_message_continue_queued,
    handle_session_plan_status,
    handle_session_rebind_project,
    handle_session_rename,
    handle_session_rewind,
    handle_session_rewind_context,
    handle_session_switch,
)

Handler = Callable[[RequestContext], Awaitable[None]]

HANDLERS: dict[ReqMethod, Handler] = {
    ReqMethod.AGENTS_LIST: handle_agents_list,
    ReqMethod.AGENTS_GET: handle_agents_get,
    ReqMethod.AGENTS_CREATE: handle_agents_create,
    ReqMethod.AGENTS_UPDATE: handle_agents_update,
    ReqMethod.AGENTS_DELETE: handle_agents_delete,
    ReqMethod.AGENTS_ENABLE: handle_agents_enable,
    ReqMethod.AGENTS_DISABLE: handle_agents_disable,
    ReqMethod.AGENTS_TOOLS_LIST: handle_agents_tools_list,
    ReqMethod.EXTENSIONS_LIST: handle_extensions_list,
    ReqMethod.EXTENSIONS_IMPORT: handle_extensions_import,
    ReqMethod.EXTENSIONS_DELETE: handle_extensions_delete,
    ReqMethod.EXTENSIONS_TOGGLE: handle_extensions_toggle,
    ReqMethod.HOOKS_LIST: handle_hooks_list,
    ReqMethod.HARNESS_PACKAGES_GET: handle_harness_packages_get,
    ReqMethod.HARNESS_PACKAGES_SCAN: handle_harness_packages_scan,
    ReqMethod.HARNESS_PACKAGES_ACTIVATE: handle_harness_packages_activate,
    ReqMethod.HARNESS_PACKAGES_DEACTIVATE: handle_harness_packages_deactivate,
    ReqMethod.HARNESS_PACKAGES_DELETE: handle_harness_packages_delete,
    ReqMethod.PERMISSIONS_TOOLS_GET: handle_permissions_config,
    ReqMethod.PERMISSIONS_TOOLS_SET: handle_permissions_config,
    ReqMethod.PERMISSIONS_TOOLS_UPDATE: handle_permissions_config,
    ReqMethod.PERMISSIONS_TOOLS_DELETE: handle_permissions_config,
    ReqMethod.PERMISSIONS_RULES_GET: handle_permissions_config,
    ReqMethod.PERMISSIONS_RULES_CREATE: handle_permissions_config,
    ReqMethod.PERMISSIONS_RULES_UPDATE: handle_permissions_config,
    ReqMethod.PERMISSIONS_RULES_DELETE: handle_permissions_config,
    ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET: handle_permissions_config,
    ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_DELETE: handle_permissions_config,
    ReqMethod.PROACTIVE_TICK: handle_proactive_tick,
    ReqMethod.PROACTIVE_FEEDBACK: handle_proactive_feedback,
    ReqMethod.BROWSER_RUNTIME_RESTART: handle_browser_runtime_restart,
    ReqMethod.CONFIG_CACHE_CLEAR: handle_config_cache_clear,
    ReqMethod.AGENT_RELOAD_CONFIG: handle_agent_reload_config,
    ReqMethod.AGENT_PREWARM_SYNC: handle_agent_prewarm_sync,
    ReqMethod.AUTH_CREDENTIALS_UPDATE: handle_auth_credentials_update,
    ReqMethod.COMMAND_WORKFLOWS: handle_command_workflows,
    ReqMethod.COMMAND_ADD_DIR: handle_command_add_dir,
    ReqMethod.COMMAND_CHROME: handle_command_chrome,
    ReqMethod.COMMAND_COMPACT: handle_command_compact,
    ReqMethod.COMMAND_COMPACT_PARTIAL: handle_command_compact_partial,
    ReqMethod.COMMAND_CONTEXT: handle_command_context,
    ReqMethod.COMMAND_RECAP: handle_command_recap,
    ReqMethod.COMMAND_BTW: handle_command_btw,
    ReqMethod.COMMAND_DIFF: handle_command_diff,
    ReqMethod.COMMAND_SIMPLIFY: handle_command_simplify,
    ReqMethod.COMMAND_MODEL: handle_command_model,
    ReqMethod.COMMAND_RESUME: handle_command_resume,
    ReqMethod.COMMAND_SESSION: handle_command_session,
    ReqMethod.COMMAND_STATUS: handle_command_status,
    ReqMethod.COMMAND_SANDBOX: handle_command_sandbox,
    ReqMethod.COMMAND_MCP: handle_command_mcp,
    ReqMethod.MCP_LIST: handle_mcp_list,
    ReqMethod.MCP_SHOW: handle_mcp_show,
    ReqMethod.MCP_INSTALL: handle_mcp_install,
    ReqMethod.MCP_UNINSTALL: handle_mcp_uninstall,
    ReqMethod.MCP_CONNECT: handle_mcp_connect,
    ReqMethod.MCP_WAIT_AUTH: handle_mcp_wait_auth,
    ReqMethod.MCP_CANCEL_CONNECT: handle_mcp_cancel_connect,
    ReqMethod.MCP_DISCONNECT: handle_mcp_disconnect,
    ReqMethod.MCP_REGISTER_CUSTOM: handle_mcp_register_custom,
    ReqMethod.MCP_DELETE_CUSTOM: handle_mcp_delete_custom,
    ReqMethod.MCP_SAVE_CREDENTIALS: handle_mcp_save_credentials,
    ReqMethod.SCHEDULE_CHECK_CONFIG: handle_schedule_request,
    ReqMethod.SCHEDULE_UPDATE_CONFIG: handle_schedule_request,
    ReqMethod.SCHEDULE_CREATE: handle_schedule_request,
    ReqMethod.SCHEDULE_RUN: handle_schedule_request,
    ReqMethod.SCHEDULE_LIST: handle_schedule_request,
    ReqMethod.SCHEDULE_STATUS: handle_schedule_request,
    ReqMethod.SCHEDULE_LOGS: handle_schedule_request,
    ReqMethod.SCHEDULE_CANCEL: handle_schedule_request,
    ReqMethod.SCHEDULE_DELETE: handle_schedule_request,
    ReqMethod.ISSUE_WATCH_ONCE: handle_schedule_request,
    ReqMethod.ISSUE_STATE_LIST: handle_schedule_request,
    ReqMethod.ISSUE_DELETE: handle_schedule_request,
    ReqMethod.ISSUE_MATRIX: handle_schedule_request,
    ReqMethod.SESSION_LIST: handle_session_list,
    ReqMethod.SESSION_RENAME: handle_session_rename,
    ReqMethod.SESSION_REBIND_PROJECT: handle_session_rebind_project,
    ReqMethod.SESSION_SWITCH: handle_session_switch,
    ReqMethod.SESSION_PLAN_STATUS: handle_session_plan_status,
    ReqMethod.SESSION_INPUT_INTENT: handle_session_input_intent,
    ReqMethod.SESSION_MESSAGE_CONTINUE_QUEUED: handle_session_message_continue_queued,
    ReqMethod.SESSION_REWIND: handle_session_rewind,
    ReqMethod.SESSION_REWIND_AND_RESTORE: handle_session_rewind,
    ReqMethod.SESSION_REWIND_COMPACT: handle_session_rewind,
    ReqMethod.SESSION_REWIND_CONTEXT: handle_session_rewind_context,
    ReqMethod.HISTORY_GET: handle_history_get,
    ReqMethod.HISTORY_APPEND_RECORD: handle_history_append_record,
}


async def try_dispatch(ctx: RequestContext) -> bool:
    """命中查表就执行并返回 True。没有登记的请求返回 False，交给原来的判断。"""

    handler = HANDLERS.get(ctx.request.req_method) if ctx.request.req_method else None
    if handler is None:
        return False
    await handler(ctx)
    return True
