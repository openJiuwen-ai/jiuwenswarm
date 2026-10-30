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
from jiuwenswarm.server.handlers.bootstrap import (
    handle_initialize,
    handle_session_create,
    handle_session_fork,
)
from jiuwenswarm.server.handlers.chat import handle_chat_cancel
from jiuwenswarm.server.handlers.cron_gateway import handle_cron_gateway_callback
from jiuwenswarm.server.handlers.organization import (
    handle_a4p_request,
    handle_org_snapshot,
    handle_team_list,
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
from jiuwenswarm.server.handlers.gateway_adapter_http import handle_gateway_adapter
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
from jiuwenswarm.server.handlers.agent_templates import handle_agent_templates_request
from jiuwenswarm.server.handlers.skills import handle_skills_request
from jiuwenswarm.server.handlers.session import (
    handle_history_append_record,
    handle_history_get,
    handle_project_sessions_lifecycle,
    handle_session_archive,
    handle_session_archived_list,
    handle_session_delete,
    handle_session_input_intent,
    handle_session_list,
    handle_session_message_continue_queued,
    handle_session_plan_status,
    handle_session_rebind_project,
    handle_session_rename,
    handle_session_rewind,
    handle_session_rewind_context,
    handle_session_switch,
    handle_session_unarchive,
)
from jiuwenswarm.server.handlers.team import (
    handle_swarmflow_control,
    handle_team_binding_create,
    handle_team_binding_generate,
    handle_team_bindings_list,
    handle_team_delete,
    handle_team_history_get,
    handle_team_members_get,
    handle_team_mq_publish,
    handle_team_session_bind,
    handle_team_snapshot,
    handle_team_templates_list,
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
    # Gateway → Agent 的 cron 回执 / 快照：企业版 HTTP 专用网址与 WS 共用。
    ReqMethod.CRON_COMMAND_ACK: handle_cron_gateway_callback,
    ReqMethod.CRON_RUN_NOW_ACK: handle_cron_gateway_callback,
    ReqMethod.CRON_JOBS_SYNC: handle_cron_gateway_callback,
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
    ReqMethod.CHAT_CANCEL: handle_chat_cancel,
    ReqMethod.INITIALIZE: handle_initialize,
    ReqMethod.SESSION_CREATE: handle_session_create,
    ReqMethod.SESSION_FORK: handle_session_fork,
    ReqMethod.SESSION_LIST: handle_session_list,
    ReqMethod.SESSION_RENAME: handle_session_rename,
    ReqMethod.SESSION_DELETE: handle_session_delete,
    # 企业版 HTTP：与 WS ``_handle_lifecycle_request`` 同契约，否则
    # Gateway mapped 路由南向落到 agentserver 时 HANDLERS 缺项 → HTTP_METHOD_NOT_HANDLED。
    ReqMethod.SESSION_ARCHIVE: handle_session_archive,
    ReqMethod.SESSION_UNARCHIVE: handle_session_unarchive,
    ReqMethod.SESSION_ARCHIVED_LIST: handle_session_archived_list,
    ReqMethod.PROJECT_SESSIONS_ARCHIVE: handle_project_sessions_lifecycle,
    ReqMethod.PROJECT_SESSIONS_DELETE_ARCHIVED: handle_project_sessions_lifecycle,
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
    ReqMethod.TEAM_TEMPLATES_LIST: handle_team_templates_list,
    ReqMethod.TEAM_BINDINGS_LIST: handle_team_bindings_list,
    ReqMethod.TEAM_BINDING_CREATE: handle_team_binding_create,
    ReqMethod.TEAM_BINDING_GENERATE: handle_team_binding_generate,
    ReqMethod.TEAM_SESSION_BIND: handle_team_session_bind,
    ReqMethod.TEAM_DELETE: handle_team_delete,
    ReqMethod.TEAM_SNAPSHOT: handle_team_snapshot,
    ReqMethod.TEAM_LIST: handle_team_list,
    ReqMethod.ORG_SNAPSHOT: handle_org_snapshot,
    ReqMethod.A4P_CONFIG_GET: handle_a4p_request,
    ReqMethod.A4P_CONFIG_UPDATE: handle_a4p_request,
    ReqMethod.TEAM_MQ_PUBLISH: handle_team_mq_publish,
    ReqMethod.TEAM_HISTORY_GET: handle_team_history_get,
    ReqMethod.TEAM_MEMBERS_GET: handle_team_members_get,
    ReqMethod.SWARMFLOW_PAUSE: handle_swarmflow_control,
    ReqMethod.SWARMFLOW_RESUME: handle_swarmflow_control,
    ReqMethod.SWARMFLOW_STOP: handle_swarmflow_control,
    # 项目域在 AgentServer 的 ProjectAdapter。WebSocket 先走适配器注册表；
    # 这里登记是为了让 HTTP 专用网址能调用同一份适配器。
    ReqMethod.PROJECT_LIST: handle_gateway_adapter,
    ReqMethod.PROJECT_INFO: handle_gateway_adapter,
    ReqMethod.PROJECT_GET_SESSIONS: handle_gateway_adapter,
    ReqMethod.PROJECT_GET_CRON_SESSIONS: handle_gateway_adapter,
    ReqMethod.PROJECT_PINNED_SESSIONS: handle_gateway_adapter,
    ReqMethod.PROJECT_CREATE: handle_gateway_adapter,
    ReqMethod.PROJECT_RENAME: handle_gateway_adapter,
    ReqMethod.PROJECT_PIN: handle_gateway_adapter,
    ReqMethod.PROJECT_REMOVE: handle_gateway_adapter,
    ReqMethod.PROJECT_RESTORE: handle_gateway_adapter,
    # 核对定时任务所属项目，以及项目现在能不能跑。都在 ProjectAdapter。
    ReqMethod.PROJECT_CRON_RESOLVE_BINDING: handle_gateway_adapter,
    ReqMethod.PROJECT_LIFECYCLE: handle_gateway_adapter,
    # 文件 HTTP 与 WebSocket 复用同一个 WorkspaceFileAdapter。
    ReqMethod.MEDIA_PERSIST: handle_gateway_adapter,
    ReqMethod.MEDIA_DISCARD: handle_gateway_adapter,
    ReqMethod.DOCUMENT_PERSIST: handle_gateway_adapter,
    ReqMethod.DOCUMENT_FORMATS: handle_gateway_adapter,
    ReqMethod.FILE_IMPORT_URL: handle_gateway_adapter,
    ReqMethod.FILE_UPLOAD_CHUNK: handle_gateway_adapter,
    ReqMethod.FILE_DOWNLOAD_VERIFIED_CHUNK: handle_gateway_adapter,
    ReqMethod.FILE_READ_UPLOAD_CHUNK: handle_gateway_adapter,
    ReqMethod.WORKSPACE_TREE: handle_gateway_adapter,
    ReqMethod.WORKSPACE_PREVIEW: handle_gateway_adapter,
    ReqMethod.WORKSPACE_USAGE: handle_gateway_adapter,
    ReqMethod.WORKSPACE_ENTRIES_DELETE: handle_gateway_adapter,
    # 语言配置在 AgentServer 的 ConfigAdapter。企业版 runtime 客户端会转发到这里。
    ReqMethod.LOCALE_GET_CONF: handle_gateway_adapter,
    ReqMethod.LOCALE_SET_CONF: handle_gateway_adapter,
    # 会话元数据和置顶都在 AgentServer 的 SessionAdapter。
    ReqMethod.SESSION_GET_METADATA: handle_gateway_adapter,
    ReqMethod.SESSION_PIN: handle_gateway_adapter,
    # 技能相关在 AgentServer 的 SkillManager。WebSocket 经 process_message；
    # 这里登记是为了让 HTTP 专用网址走同一条路径（method_has_http_handler 依赖本表）。
    # 凡 pod_rest_map.REST_ROUTES 中的 skills.* 都必须登记，否则企业版
    # Gateway→Pod HTTP 会直接回 HTTP_METHOD_NOT_HANDLED（例如 skills.toggle）。
    ReqMethod.SKILLS_LIST: handle_skills_request,
    ReqMethod.SKILLS_INSTALLED: handle_skills_request,
    ReqMethod.SKILLS_GET: handle_skills_request,
    ReqMethod.SKILLS_FILES_LIST: handle_skills_request,
    ReqMethod.SKILLS_FILES_GET: handle_skills_request,
    ReqMethod.SKILLS_VERSIONS_LIST: handle_skills_request,
    ReqMethod.SKILLS_TOGGLE: handle_skills_request,
    ReqMethod.SKILLS_INSTALL: handle_skills_request,
    ReqMethod.SKILLS_IMPORT_LOCAL: handle_skills_request,
    ReqMethod.SKILLS_UNINSTALL: handle_skills_request,
    ReqMethod.SKILLS_MARKETPLACE_LIST: handle_skills_request,
    ReqMethod.SKILLS_MARKETPLACE_ADD: handle_skills_request,
    ReqMethod.SKILLS_MARKETPLACE_REMOVE: handle_skills_request,
    ReqMethod.SKILLS_MARKETPLACE_TOGGLE: handle_skills_request,
    ReqMethod.SKILLS_ONLINE_SEARCH: handle_skills_request,
    ReqMethod.SKILLS_SKILLNET_SEARCH: handle_skills_request,
    ReqMethod.SKILLS_SKILLNET_INSTALL: handle_skills_request,
    ReqMethod.SKILLS_SKILLNET_INSTALL_STATUS: handle_skills_request,
    ReqMethod.SKILLS_SKILLNET_EVALUATE: handle_skills_request,
    ReqMethod.SKILLS_CLAWHUB_GET_TOKEN: handle_skills_request,
    ReqMethod.SKILLS_CLAWHUB_SET_TOKEN: handle_skills_request,
    ReqMethod.SKILLS_CLAWHUB_SEARCH: handle_skills_request,
    ReqMethod.SKILLS_CLAWHUB_DOWNLOAD: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INFO: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INIT: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_VALIDATE: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_PACK: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_SEARCH: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INSTALL: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_PUBLISH: handle_skills_request,
    ReqMethod.SKILLS_TEAMSKILLS_HUB_DELETE: handle_skills_request,
    ReqMethod.SKILLS_RETRIEVAL_STATUS: handle_skills_request,
    ReqMethod.SKILLS_RETRIEVAL_INDEX_BUILD: handle_skills_request,
    ReqMethod.SKILLS_RETRIEVAL_INDEX_CANCEL: handle_skills_request,
    ReqMethod.SKILLS_RETRIEVAL_SEARCH: handle_skills_request,
    ReqMethod.SKILLS_RETRIEVAL_TREE: handle_skills_request,
    ReqMethod.SKILLS_EVOLUTION_STATUS: handle_skills_request,
    ReqMethod.SKILLS_EVOLUTION_GET: handle_skills_request,
    ReqMethod.SKILLS_EVOLUTION_SAVE: handle_skills_request,
    ReqMethod.SKILLS_SOURCE_PROVIDERS: handle_skills_request,
    ReqMethod.SKILLS_SOURCE_SEARCH: handle_skills_request,
    ReqMethod.SKILLS_SOURCE_INSTALL: handle_skills_request,
    ReqMethod.SKILLS_UPDATES_CHECK: handle_skills_request,
    ReqMethod.SKILLS_UPDATE: handle_skills_request,
    ReqMethod.SKILLS_ENTERPRISE_LIST: handle_skills_request,
    ReqMethod.SKILLS_ENTERPRISE_INSTALL: handle_skills_request,
    ReqMethod.SKILLS_ENTERPRISE_UNINSTALL: handle_skills_request,
    ReqMethod.SKILLS_ENTERPRISE_SOURCE_PROVIDERS: handle_skills_request,
    ReqMethod.SKILLS_ENTERPRISE_SOURCE_SEARCH: handle_skills_request,
    # 专家包在 AgentServer 的装备目录。WebSocket 经 process_message；
    # 企业版南向是 HTTP，这里登记后专用网址走同一条路径。
    ReqMethod.AGENT_TEMPLATES_LIST: handle_agent_templates_request,
    ReqMethod.AGENT_TEMPLATES_SHOW: handle_agent_templates_request,
    ReqMethod.AGENT_TEMPLATES_FILE_LIST: handle_agent_templates_request,
    ReqMethod.AGENT_TEMPLATES_FILE_READ: handle_agent_templates_request,
    ReqMethod.AGENT_TEMPLATES_INSTALL: handle_agent_templates_request,
    ReqMethod.AGENT_TEMPLATES_UNINSTALL: handle_agent_templates_request,
}


async def try_dispatch(ctx: RequestContext) -> bool:
    """命中查表就执行并返回 True。没有登记的请求返回 False，交给原来的判断。"""

    handler = HANDLERS.get(ctx.request.req_method) if ctx.request.req_method else None
    if handler is None:
        return False
    from jiuwenswarm.server.context import bind_request_tenant

    async with bind_request_tenant(ctx):
        await handler(ctx)
    return True
