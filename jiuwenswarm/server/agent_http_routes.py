# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 路由表：RESTful资源路径 → ``ReqMethod``。

所有路由最终都汇聚到 ``AgentHTTPServer.invoke_unary`` / ``iter_stream``，
再经 ``AgentWebSocketServer._handle_message``
新增接口通常只需在 :data:`ROUTES` 加一行；只有需要流式或特殊参数处理的
接口才单独写函数（见 ``_register_special_routes``）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# NOTE: 这些必须是**模块级**导入。本模块启用了 ``from __future__ import annotations``，
# 注解在运行时是字符串，FastAPI 通过 ``get_type_hints()`` 在模块全局命名空间解析；
# 若把 ``Request`` 放在函数内导入，FastAPI 解析不到就会把它当查询参数，导致 422。
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_http_server import (
    _as_bool,
    _frame_event_name,
    API_PREFIX,
    AgentHTTPServer,
    is_valid_req_method,
    new_request_id,
    resolve_cors_origins,
)

logger = logging.getLogger(__name__)


#: ``param_defaults`` 里的占位符：取本次请求的 request_id。
#: 用它可让「同一 X-Request-Id 重复调用」天然幂等。
REQUEST_ID_PLACEHOLDER = "$request_id"


@dataclass(frozen=True)
class RequestContext:
    """HTTP 请求身份与租户键。"""

    request_id: str
    channel_id: str
    session_id: str | None
    user_id: str | None
    routing: dict[str, str]
    tenant_ids: dict[str, str]
    request_ext: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteSpec:
    """一条声明式路由。

    Attributes:
        verb: HTTP 动词。
        path: 相对 ``API_PREFIX`` 的路径，可含 ``{}`` 路径参数。
        method: 对应的 ``ReqMethod`` 值。
        status: 成功时的 HTTP 状态码（``ok=True`` 时生效）。
        param_defaults: 调用方未提供时补齐的 ``params`` 默认值。值为
            :data:`REQUEST_ID_PLACEHOLDER` 时替换为本次 request_id。
            用于 WS 客户端（前端）会传、但 RESTful 调用方不该被迫关心的参数。
    """

    verb: str
    path: str
    method: str
    status: int = 200
    param_defaults: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 声明式路由表（非流式）。流式接口见 _register_special_routes。
# ---------------------------------------------------------------------------
ROUTES: list[RouteSpec] = [
    # --- 初始化 ---
    RouteSpec("POST", "/initialize", ReqMethod.INITIALIZE.value),
    # --- 会话 ---
    RouteSpec("GET", "/sessions", ReqMethod.SESSION_LIST.value),
    # create_token：AgentServer 用它认领预热会话（缺失会 ValueError）。
    # WS 前端自己生成；RESTful 调用方不该被迫关心，缺省用 request_id 补齐，
    # 这样同一 X-Request-Id 重试天然幂等。
    RouteSpec(
        "POST",
        "/sessions",
        ReqMethod.SESSION_CREATE.value,
        201,
        param_defaults={"create_token": REQUEST_ID_PLACEHOLDER},
    ),
    RouteSpec("PATCH", "/sessions/{session_id}", ReqMethod.SESSION_RENAME.value),
    RouteSpec("DELETE", "/sessions/{session_id}", ReqMethod.SESSION_DELETE.value),
    RouteSpec("POST", "/sessions/{session_id}/actions/switch", ReqMethod.SESSION_SWITCH.value),
    RouteSpec("POST", "/sessions/{session_id}/actions/fork", ReqMethod.SESSION_FORK.value, 201),
    RouteSpec("POST", "/sessions/{session_id}/actions/rewind", ReqMethod.SESSION_REWIND.value),
    RouteSpec(
        "POST",
        "/sessions/{session_id}/actions/rewind-restore",
        ReqMethod.SESSION_REWIND_AND_RESTORE.value,
    ),
    RouteSpec(
        "POST",
        "/sessions/{session_id}/actions/rewind-compact",
        ReqMethod.SESSION_REWIND_COMPACT.value,
    ),
    RouteSpec(
        "POST",
        "/sessions/{session_id}/actions/rewind-context",
        ReqMethod.SESSION_REWIND_CONTEXT.value,
    ),
    RouteSpec(
        "POST",
        "/sessions/{session_id}/actions/restore-files",
        ReqMethod.SESSION_RESTORE_FILES.value,
    ),
    RouteSpec("GET", "/sessions/{session_id}/history", ReqMethod.HISTORY_GET.value),
    RouteSpec("GET", "/sessions/{session_id}/turns", ReqMethod.HISTORY_LIST_TURNS.value),
    # --- 对话动作（非流式） ---
    RouteSpec("POST", "/chat/{session_id}/actions/interrupt", ReqMethod.CHAT_CANCEL.value),
    RouteSpec("POST", "/chat/{session_id}/actions/answer", ReqMethod.CHAT_ANSWER.value),
    # --- 命令 ---
    RouteSpec("POST", "/sessions/{session_id}/commands", ReqMethod.COMMAND_SESSION.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/compact", ReqMethod.COMMAND_COMPACT.value),
    RouteSpec(
        "POST",
        "/sessions/{session_id}/commands/compact-partial",
        ReqMethod.COMMAND_COMPACT_PARTIAL.value,
    ),
    RouteSpec("POST", "/sessions/{session_id}/commands/model", ReqMethod.COMMAND_MODEL.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/mcp", ReqMethod.COMMAND_MCP.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/sandbox", ReqMethod.COMMAND_SANDBOX.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/btw", ReqMethod.COMMAND_BTW.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/add-dir", ReqMethod.COMMAND_ADD_DIR.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/chrome", ReqMethod.COMMAND_CHROME.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/context", ReqMethod.COMMAND_CONTEXT.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/recap", ReqMethod.COMMAND_RECAP.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/diff", ReqMethod.COMMAND_DIFF.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/simplify", ReqMethod.COMMAND_SIMPLIFY.value),
    RouteSpec("POST", "/sessions/{session_id}/commands/resume", ReqMethod.COMMAND_RESUME.value),
    RouteSpec(
        "POST", "/sessions/{session_id}/commands/workflows", ReqMethod.COMMAND_WORKFLOWS.value
    ),
    RouteSpec("POST", "/sessions/{session_id}/commands/goal", ReqMethod.COMMAND_GOAL.value),
    RouteSpec("GET", "/sessions/{session_id}/commands/status", ReqMethod.COMMAND_STATUS.value),
    # --- 智能体 ---
    RouteSpec("GET", "/agents", ReqMethod.AGENTS_LIST.value),
    RouteSpec("POST", "/agents", ReqMethod.AGENTS_CREATE.value, 201),
    RouteSpec("GET", "/agents/{name}", ReqMethod.AGENTS_GET.value),
    RouteSpec("PUT", "/agents/{name}", ReqMethod.AGENTS_UPDATE.value),
    RouteSpec("DELETE", "/agents/{name}", ReqMethod.AGENTS_DELETE.value),
    RouteSpec("POST", "/agents/{name}/actions/enable", ReqMethod.AGENTS_ENABLE.value),
    RouteSpec("POST", "/agents/{name}/actions/disable", ReqMethod.AGENTS_DISABLE.value),
    RouteSpec("GET", "/agents/{name}/tools", ReqMethod.AGENTS_TOOLS_LIST.value),
    # --- 团队 ---
    RouteSpec("GET", "/teams/templates", ReqMethod.TEAM_TEMPLATES_LIST.value),
    RouteSpec("GET", "/teams/bindings", ReqMethod.TEAM_BINDINGS_LIST.value),
    RouteSpec("POST", "/teams/bindings", ReqMethod.TEAM_BINDING_CREATE.value, 201),
    RouteSpec(
        "POST", "/teams/bindings/actions/generate", ReqMethod.TEAM_BINDING_GENERATE.value
    ),
    RouteSpec(
        "POST",
        "/teams/{team_name}/sessions/{session_id}/bind",
        ReqMethod.TEAM_SESSION_BIND.value,
    ),
    # 这三个都是**按会话**查询（handler 读 params.session_id），因此挂在会话下。
    RouteSpec("GET", "/sessions/{session_id}/team/snapshot", ReqMethod.TEAM_SNAPSHOT.value),
    RouteSpec("GET", "/sessions/{session_id}/team/members", ReqMethod.TEAM_MEMBERS_GET.value),
    RouteSpec("GET", "/sessions/{session_id}/team/history", ReqMethod.TEAM_HISTORY_GET.value),
    RouteSpec("POST", "/teams/mq/publish", ReqMethod.TEAM_MQ_PUBLISH.value),
    RouteSpec("DELETE", "/teams/{team_name}", ReqMethod.TEAM_DELETE.value),
    # --- 技能 ---
    RouteSpec("GET", "/skills", ReqMethod.SKILLS_LIST.value),
    RouteSpec("GET", "/skills/installed", ReqMethod.SKILLS_INSTALLED.value),
    RouteSpec("GET", "/skills/marketplace", ReqMethod.SKILLS_MARKETPLACE_LIST.value),
    RouteSpec("POST", "/skills/marketplace", ReqMethod.SKILLS_MARKETPLACE_ADD.value, 201),
    RouteSpec(
        "DELETE", "/skills/marketplace/{name}", ReqMethod.SKILLS_MARKETPLACE_REMOVE.value
    ),
    RouteSpec(
        "POST",
        "/skills/marketplace/{name}/actions/toggle",
        ReqMethod.SKILLS_MARKETPLACE_TOGGLE.value,
    ),
    RouteSpec("POST", "/skills/install", ReqMethod.SKILLS_INSTALL.value, 201),
    RouteSpec("POST", "/skills/import-local", ReqMethod.SKILLS_IMPORT_LOCAL.value, 201),
    RouteSpec("POST", "/skills/online-search", ReqMethod.SKILLS_ONLINE_SEARCH.value),
    RouteSpec("GET", "/skills/retrieval/status", ReqMethod.SKILLS_RETRIEVAL_STATUS.value),
    RouteSpec(
        "POST", "/skills/retrieval/index-build", ReqMethod.SKILLS_RETRIEVAL_INDEX_BUILD.value
    ),
    RouteSpec(
        "POST", "/skills/retrieval/index-cancel", ReqMethod.SKILLS_RETRIEVAL_INDEX_CANCEL.value
    ),
    RouteSpec("GET", "/skills/retrieval/search", ReqMethod.SKILLS_RETRIEVAL_SEARCH.value),
    RouteSpec("GET", "/skills/retrieval/tree", ReqMethod.SKILLS_RETRIEVAL_TREE.value),
    RouteSpec("GET", "/skills/evolution", ReqMethod.SKILLS_EVOLUTION_GET.value),
    RouteSpec("PUT", "/skills/evolution", ReqMethod.SKILLS_EVOLUTION_SAVE.value),
    # evolution.status 是**按技能**查询（handler 要求 params.name），
    # 因此挂在具体技能下而非全局路径。
    RouteSpec(
        "GET", "/skills/{name}/evolution/status", ReqMethod.SKILLS_EVOLUTION_STATUS.value
    ),
    RouteSpec("GET", "/skills/evolution/archives", ReqMethod.SKILLS_EVOLUTION_ARCHIVES.value),
    RouteSpec(
        "POST", "/skills/evolution/actions/rollback", ReqMethod.SKILLS_EVOLUTION_ROLLBACK.value
    ),
    RouteSpec(
        "POST", "/skills/evolution/actions/rebuild", ReqMethod.SKILLS_EVOLUTION_REBUILD.value
    ),
    # --- 技能来源：clawhub / skillnet / teamskillshub / enterprise ---
    # 注意：以下路径必须声明在 `/skills/{name}` **之前**。Starlette 按注册顺序匹配，
    # 放到后面会被 `{name}` 吞掉（`/skills/clawhub/search` 会当成 name="clawhub"）。
    RouteSpec("GET", "/skills/clawhub/token", ReqMethod.SKILLS_CLAWHUB_GET_TOKEN.value),
    RouteSpec("PUT", "/skills/clawhub/token", ReqMethod.SKILLS_CLAWHUB_SET_TOKEN.value),
    RouteSpec("GET", "/skills/clawhub/search", ReqMethod.SKILLS_CLAWHUB_SEARCH.value),
    RouteSpec(
        "POST", "/skills/clawhub/actions/download", ReqMethod.SKILLS_CLAWHUB_DOWNLOAD.value
    ),
    RouteSpec("GET", "/skills/skillnet/search", ReqMethod.SKILLS_SKILLNET_SEARCH.value),
    RouteSpec("POST", "/skills/skillnet/install", ReqMethod.SKILLS_SKILLNET_INSTALL.value, 201),
    RouteSpec(
        "GET",
        "/skills/skillnet/install-status",
        ReqMethod.SKILLS_SKILLNET_INSTALL_STATUS.value,
    ),
    RouteSpec(
        "POST", "/skills/skillnet/actions/evaluate", ReqMethod.SKILLS_SKILLNET_EVALUATE.value
    ),
    RouteSpec(
        "GET", "/skills/teamskillshub/info", ReqMethod.SKILLS_TEAMSKILLS_HUB_INFO.value
    ),
    RouteSpec(
        "GET", "/skills/teamskillshub/search", ReqMethod.SKILLS_TEAMSKILLS_HUB_SEARCH.value
    ),
    RouteSpec(
        "POST",
        "/skills/teamskillshub/install",
        ReqMethod.SKILLS_TEAMSKILLS_HUB_INSTALL.value,
        201,
    ),
    RouteSpec(
        "POST", "/skills/teamskillshub/actions/init", ReqMethod.SKILLS_TEAMSKILLS_HUB_INIT.value
    ),
    RouteSpec(
        "POST",
        "/skills/teamskillshub/actions/validate",
        ReqMethod.SKILLS_TEAMSKILLS_HUB_VALIDATE.value,
    ),
    RouteSpec(
        "POST", "/skills/teamskillshub/actions/pack", ReqMethod.SKILLS_TEAMSKILLS_HUB_PACK.value
    ),
    RouteSpec(
        "POST",
        "/skills/teamskillshub/actions/publish",
        ReqMethod.SKILLS_TEAMSKILLS_HUB_PUBLISH.value,
    ),
    # 删除的标识（asset_id）在 body 里而不是路径里，与 WS 侧参数保持一致，
    # 因此用 actions/delete 而非 DELETE /{id}。
    RouteSpec(
        "POST",
        "/skills/teamskillshub/actions/delete",
        ReqMethod.SKILLS_TEAMSKILLS_HUB_DELETE.value,
    ),
    RouteSpec(
        "GET", "/skills/sources", ReqMethod.SKILLS_SOURCE_PROVIDERS.value
    ),
    RouteSpec(
        "GET", "/skills/sources/search", ReqMethod.SKILLS_SOURCE_SEARCH.value
    ),
    RouteSpec(
        "POST", "/skills/sources/install", ReqMethod.SKILLS_SOURCE_INSTALL.value, 201
    ),
    RouteSpec(
        "GET", "/skills/updates", ReqMethod.SKILLS_UPDATES_CHECK.value
    ),
    RouteSpec(
        "POST", "/skills/actions/update", ReqMethod.SKILLS_UPDATE.value
    ),
    RouteSpec(
        "GET", "/skills/enterprise", ReqMethod.SKILLS_ENTERPRISE_LIST.value
    ),
    RouteSpec(
        "POST", "/skills/enterprise/install", ReqMethod.SKILLS_ENTERPRISE_INSTALL.value, 201
    ),
    RouteSpec(
        "POST",
        "/skills/enterprise/actions/uninstall",
        ReqMethod.SKILLS_ENTERPRISE_UNINSTALL.value,
    ),
    RouteSpec(
        "GET",
        "/skills/enterprise/sources",
        ReqMethod.SKILLS_ENTERPRISE_SOURCE_PROVIDERS.value,
    ),
    RouteSpec(
        "GET",
        "/skills/enterprise/sources/search",
        ReqMethod.SKILLS_ENTERPRISE_SOURCE_SEARCH.value,
    ),
    RouteSpec("GET", "/skills/{name}", ReqMethod.SKILLS_GET.value),
    RouteSpec("DELETE", "/skills/{name}", ReqMethod.SKILLS_UNINSTALL.value),
    RouteSpec("POST", "/skills/{name}/actions/toggle", ReqMethod.SKILLS_TOGGLE.value),
    # --- 扩展 / Hooks / 插件 ---
    RouteSpec("GET", "/extensions", ReqMethod.EXTENSIONS_LIST.value),
    RouteSpec("POST", "/extensions", ReqMethod.EXTENSIONS_IMPORT.value, 201),
    RouteSpec("DELETE", "/extensions/{name}", ReqMethod.EXTENSIONS_DELETE.value),
    RouteSpec("POST", "/extensions/{name}/actions/toggle", ReqMethod.EXTENSIONS_TOGGLE.value),
    RouteSpec("GET", "/hooks", ReqMethod.HOOKS_LIST.value),
    RouteSpec("GET", "/plugins", ReqMethod.PLUGINS_LIST.value),
    RouteSpec("POST", "/plugins/install", ReqMethod.PLUGINS_INSTALL.value, 201),
    RouteSpec("POST", "/plugins/actions/reload", ReqMethod.PLUGINS_RELOAD.value),
    RouteSpec("DELETE", "/plugins/{name}", ReqMethod.PLUGINS_UNINSTALL.value),
    RouteSpec("POST", "/plugins/{name}/actions/enable", ReqMethod.PLUGINS_ENABLE.value),
    RouteSpec("POST", "/plugins/{name}/actions/disable", ReqMethod.PLUGINS_DISABLE.value),
    # --- 调度 / 议题 ---
    RouteSpec("GET", "/schedule/config", ReqMethod.SCHEDULE_CHECK_CONFIG.value),
    RouteSpec("PATCH", "/schedule/config", ReqMethod.SCHEDULE_UPDATE_CONFIG.value),
    RouteSpec("GET", "/schedule/tasks", ReqMethod.SCHEDULE_LIST.value),
    RouteSpec("POST", "/schedule/tasks", ReqMethod.SCHEDULE_CREATE.value, 201),
    RouteSpec("POST", "/schedule/tasks/actions/run", ReqMethod.SCHEDULE_RUN.value),
    RouteSpec("GET", "/schedule/tasks/{task_id}", ReqMethod.SCHEDULE_STATUS.value),
    RouteSpec("GET", "/schedule/tasks/{task_id}/logs", ReqMethod.SCHEDULE_LOGS.value),
    RouteSpec(
        "POST", "/schedule/tasks/{task_id}/actions/cancel", ReqMethod.SCHEDULE_CANCEL.value
    ),
    RouteSpec("DELETE", "/schedule/tasks/{task_id}", ReqMethod.SCHEDULE_DELETE.value),
    RouteSpec("POST", "/issues/actions/watch-once", ReqMethod.ISSUE_WATCH_ONCE.value),
    RouteSpec("GET", "/issues/states", ReqMethod.ISSUE_STATE_LIST.value),
    RouteSpec("POST", "/issues/actions/matrix", ReqMethod.ISSUE_MATRIX.value),
    RouteSpec("DELETE", "/issues/{issue_id}", ReqMethod.ISSUE_DELETE.value),
    # --- Harness 包 ---
    RouteSpec("GET", "/harness/packages", ReqMethod.HARNESS_PACKAGES_GET.value),
    RouteSpec("POST", "/harness/packages/scan", ReqMethod.HARNESS_PACKAGES_SCAN.value),
    RouteSpec(
        "POST",
        "/harness/packages/{name}/actions/activate",
        ReqMethod.HARNESS_PACKAGES_ACTIVATE.value,
    ),
    RouteSpec(
        "POST",
        "/harness/packages/{name}/actions/deactivate",
        ReqMethod.HARNESS_PACKAGES_DEACTIVATE.value,
    ),
    RouteSpec("DELETE", "/harness/packages/{name}", ReqMethod.HARNESS_PACKAGES_DELETE.value),
    # --- 权限 ---
    RouteSpec("GET", "/permissions/enabled", ReqMethod.PERMISSIONS_ENABLED_GET.value),
    RouteSpec("PUT", "/permissions/enabled", ReqMethod.PERMISSIONS_ENABLED_SET.value),
    RouteSpec("GET", "/permissions/tools", ReqMethod.PERMISSIONS_TOOLS_GET.value),
    RouteSpec("GET", "/permissions/tools/list", ReqMethod.PERMISSIONS_TOOLS_LIST.value),
    RouteSpec("PUT", "/permissions/tools", ReqMethod.PERMISSIONS_TOOLS_SET.value),
    RouteSpec("PATCH", "/permissions/tools", ReqMethod.PERMISSIONS_TOOLS_UPDATE.value),
    RouteSpec("DELETE", "/permissions/tools/{rule_id}", ReqMethod.PERMISSIONS_TOOLS_DELETE.value),
    RouteSpec("GET", "/permissions/rules", ReqMethod.PERMISSIONS_RULES_GET.value),
    RouteSpec("POST", "/permissions/rules", ReqMethod.PERMISSIONS_RULES_CREATE.value, 201),
    RouteSpec("PATCH", "/permissions/rules/{rule_id}", ReqMethod.PERMISSIONS_RULES_UPDATE.value),
    RouteSpec("DELETE", "/permissions/rules/{rule_id}", ReqMethod.PERMISSIONS_RULES_DELETE.value),
    RouteSpec(
        "GET",
        "/permissions/approval-overrides",
        ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET.value,
    ),
    RouteSpec(
        "DELETE",
        "/permissions/approval-overrides/{override_id}",
        ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_DELETE.value,
    ),
    RouteSpec(
        "GET",
        "/permissions/file-guard/workspace",
        ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_GET.value,
    ),
    RouteSpec(
        "PUT",
        "/permissions/file-guard/workspace",
        ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_SET.value,
    ),
    RouteSpec(
        "GET",
        "/permissions/file-guard/workspace/access",
        ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET.value,
    ),
    RouteSpec(
        "PUT",
        "/permissions/file-guard/workspace/access",
        ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_SET.value,
    ),
    # --- 配置与运维 ---
    RouteSpec("POST", "/config/actions/cache-clear", ReqMethod.CONFIG_CACHE_CLEAR.value),
    RouteSpec("POST", "/config/actions/agent-reload", ReqMethod.AGENT_RELOAD_CONFIG.value),
    RouteSpec("POST", "/config/actions/sync-agents", ReqMethod.SYNC_AGENTS_CONFIGS.value),
    RouteSpec("POST", "/config/actions/prewarm-sync", ReqMethod.AGENT_PREWARM_SYNC.value),
    RouteSpec("POST", "/runtime/browser/actions/restart", ReqMethod.BROWSER_RUNTIME_RESTART.value),
    RouteSpec("POST", "/proactive/actions/tick", ReqMethod.PROACTIVE_TICK.value),
    RouteSpec("POST", "/acp/tool-responses", ReqMethod.ACP_TOOL_RESPONSE.value),
    # --- Symphony（乐谱式编排）---
    RouteSpec("POST", "/symphony/actions/plan", ReqMethod.SYMPHONY_PLAN.value),
    RouteSpec("POST", "/symphony/actions/build-score", ReqMethod.SYMPHONY_BUILD_SCORE.value),
    RouteSpec("POST", "/symphony/actions/pause-build", ReqMethod.SYMPHONY_PAUSE_BUILD.value),
    RouteSpec("GET", "/symphony/score-status", ReqMethod.SYMPHONY_SCORE_STATUS.value),
    RouteSpec("GET", "/symphony/graph", ReqMethod.SYMPHONY_GRAPH.value),
    # --- 渠道配置 ---
    RouteSpec("GET", "/channels/feishu/config", ReqMethod.CHANNEL_FEISHU_GET_CONF.value),
    RouteSpec("PUT", "/channels/feishu/config", ReqMethod.CHANNEL_FEISHU_SET_CONF.value),
    RouteSpec("GET", "/channels/xiaoyi/config", ReqMethod.CHANNEL_XIAOYI_GET_CONF.value),
    RouteSpec("PUT", "/channels/xiaoyi/config", ReqMethod.CHANNEL_XIAOYI_SET_CONF.value),
    RouteSpec("GET", "/channels/telegram/config", ReqMethod.CHANNEL_TELEGRAM_GET_CONF.value),
    RouteSpec("PUT", "/channels/telegram/config", ReqMethod.CHANNEL_TELEGRAM_SET_CONF.value),
    RouteSpec("GET", "/channels/slack/config", ReqMethod.CHANNEL_SLACK_GET_CONF.value),
    RouteSpec("PUT", "/channels/slack/config", ReqMethod.CHANNEL_SLACK_SET_CONF.value),
    RouteSpec("GET", "/channels/dingtalk/config", ReqMethod.CHANNEL_DINGTALK_GET_CONF.value),
    RouteSpec("PUT", "/channels/dingtalk/config", ReqMethod.CHANNEL_DINGTALK_SET_CONF.value),
    RouteSpec("GET", "/channels/whatsapp/config", ReqMethod.CHANNEL_WHATSAPP_GET_CONF.value),
    RouteSpec("PUT", "/channels/whatsapp/config", ReqMethod.CHANNEL_WHATSAPP_SET_CONF.value),
    RouteSpec("GET", "/channels/wechat/config", ReqMethod.CHANNEL_WECHAT_GET_CONF.value),
    RouteSpec("PUT", "/channels/wechat/config", ReqMethod.CHANNEL_WECHAT_SET_CONF.value),
    RouteSpec("GET", "/channels/wechat/login-ui", ReqMethod.CHANNEL_WECHAT_GET_LOGIN_UI.value),
    RouteSpec("POST", "/channels/wechat/actions/unbind", ReqMethod.CHANNEL_WECHAT_UNBIND.value),
    # --- 更新器 / 心跳 ---
    RouteSpec("GET", "/updater/status", ReqMethod.UPDATER_GET_STATUS.value),
    RouteSpec("POST", "/updater/actions/check", ReqMethod.UPDATER_CHECK.value),
    RouteSpec("POST", "/updater/actions/download", ReqMethod.UPDATER_DOWNLOAD.value),
    RouteSpec("GET", "/updater/config", ReqMethod.UPDATER_GET_CONF.value),
    RouteSpec("PUT", "/updater/config", ReqMethod.UPDATER_SET_CONF.value),
    RouteSpec("GET", "/heartbeat/config", ReqMethod.HEARTBEAT_GET_CONF.value),
    RouteSpec("PUT", "/heartbeat/config", ReqMethod.HEARTBEAT_SET_CONF.value),
]


async def _safe_json_body(request: Any) -> dict[str, Any]:
    """读取 JSON body；空体或非法 JSON 返回 {}。"""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return {}
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[AgentHTTPRoutes] 请求体非法 JSON，已忽略")
        return {}
    return data if isinstance(data, dict) else {"body": data}


async def collect_params(request: Any) -> dict[str, Any]:
    """合并 query / path / body 为单一 ``params`` 字典。

    优先级：body > path > query（body 最贴近调用者意图）。
    """
    params: dict[str, Any] = dict(request.query_params)
    params.update(dict(request.path_params or {}))
    params.update(await _safe_json_body(request))
    return params


def request_context(request: Any) -> RequestContext:
    """从请求头/路径提取身份与租户键。

    ``routing`` 含 ``X-User-Id`` 与 ``X-Group/Bot/Gateway-Id``，由
    :func:`apply_routing_metadata` 拆到顶层 ``user_id`` + ``metadata.routing``。
    ``gateway_id`` 仅重建保留，Agent 业务不强制消费。

    ``tenant_ids`` 含可选的 ``service_id`` / ``agent_id`` / ``workspace_key``
    （来自 ``X-Service-Id`` / ``X-Agent-Id`` / ``X-Workspace-Key``）。
    """
    from jiuwenswarm.common.request_ext import (
        INTERNAL_HEADER_NAME,
        RequestExtCodecError,
        decode_internal_header,
    )
    from jiuwenswarm.common.request_identity import normalize_routing_identity

    headers = request.headers
    request_id = headers.get("x-request-id") or new_request_id()
    channel_id = headers.get("x-channel-id") or "web"
    session_id = (request.path_params or {}).get("session_id") or headers.get("x-session-id")
    user_id = headers.get("x-user-id")
    routing = normalize_routing_identity(
        {
            "user_id": user_id,
            "group_id": headers.get("x-group-id"),
            "bot_id": headers.get("x-bot-id"),
            "gateway_id": headers.get("x-gateway-id"),
        }
    )
    tenant_ids: dict[str, str] = {}
    for header_name, tenant_key in (
        ("x-service-id", "service_id"),
        ("x-agent-id", "agent_id"),
        ("x-workspace-key", "workspace_key"),
    ):
        raw = headers.get(header_name)
        text = str(raw).strip() if raw is not None else ""
        if text:
            tenant_ids[tenant_key] = text
    try:
        request_ext = decode_internal_header(headers.get(INTERNAL_HEADER_NAME)) or {}
    except RequestExtCodecError as exc:
        logger.warning("[AgentHTTPRoutes] 非法内部扩展字段头: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="invalid internal request extension header",
        ) from exc
    return RequestContext(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        user_id=user_id,
        routing=routing,
        tenant_ids=tenant_ids,
        request_ext=request_ext,
    )


def _envelope_wants_stream(request: Any, envelope: dict[str, Any]) -> bool:
    """``/e2a`` 是否该走 SSE。

    以**信封里的 ``is_stream``** 为准 —— ``/e2a`` 的语义是"原样透传客户端给的信封"，
    而 WS 侧决定流式与否的正是这个字段，照它办才叫对齐。

    额外接受 ``Accept: text/event-stream``：信封没声明流式、但调用方想按事件流读
    单帧响应时也能满足，行为可预期（一条 data 事件后结束）。
    """
    if _as_bool(envelope.get("is_stream")):
        return True
    return "text/event-stream" in (request.headers.get("accept") or "").lower()


def wants_stream(request: Any, params: dict[str, Any]) -> bool:
    """判定是否走 SSE：``Accept: text/event-stream`` 或 ``enable_streaming``。"""
    accept = (request.headers.get("accept") or "").lower()
    if "text/event-stream" in accept:
        return True
    flag = params.get("enable_streaming")
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return False


def build_fastapi_app(server: AgentHTTPServer) -> Any:
    """构建 FastAPI 应用并注册全部路由。"""
    from jiuwenswarm.common.security.link_mtls import (
        LinkMTLSConfig,
    )

    link_mtls = LinkMTLSConfig.from_env(role="agentserver")
    response_type = EventSourceResponse
    if link_mtls.enforced:
        from jiuwenswarm.server.transports.link_sse_response import LinkEventSourceResponse

        response_type = LinkEventSourceResponse
    app = FastAPI(
        title="JiuwenSwarm AgentServer HTTP API",
        version="1.0.0",
        description="与 WebSocket 能力对齐的 RESTful + SSE 接口",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    # 默认只放行本机前端（端口按 FRONTEND_PORT / WEB_PORT 推导，多实例不写死）。
    # 想放开或指定域名：env AGENT_HTTP_CORS_ORIGINS，或 config.yaml 的
    # http_server.cors_origins。详见 resolve_cors_origins 的 docstring。
    cors_origins, cors_credentials = resolve_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if link_mtls.enforced:
        from openjiuwen_runtime.foundation.security.link_stream_guard import LinkBindingMiddleware

        app.add_middleware(LinkBindingMiddleware, authorize=link_mtls.authorize_request)

    def _make_endpoint(spec: RouteSpec) -> Callable:
        async def endpoint(request: Request) -> JSONResponse:
            params = await collect_params(request)
            ctx = request_context(request)
            for key, default in spec.param_defaults.items():
                if not params.get(key):
                    params[key] = (
                        ctx.request_id if default == REQUEST_ID_PLACEHOLDER else default
                    )
            payload, status = await server.invoke_unary(
                spec.method,
                params,
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                channel_id=ctx.channel_id,
                user_id=ctx.user_id,
                routing=ctx.routing,
                tenant_ids=ctx.tenant_ids,
                request_ext=ctx.request_ext,
            )
            if payload.get("ok") and status == 200:
                status = spec.status
            return JSONResponse(
                payload, status_code=status, headers={"X-Request-Id": ctx.request_id}
            )

        endpoint.__name__ = f"{spec.verb.lower()}_{spec.method.replace('.', '_')}"
        return endpoint

    for spec in ROUTES:
        app.add_api_route(
            f"{API_PREFIX}{spec.path}",
            _make_endpoint(spec),
            methods=[spec.verb],
            name=f"{spec.verb} {spec.path}",
        )

    _register_special_routes(app, server, response_type=response_type)
    logger.info("[AgentHTTPRoutes] 已注册 %d 条声明式路由 + 特殊路由", len(ROUTES))
    return app


def _register_special_routes(
    app: Any, server: AgentHTTPServer, *, response_type=EventSourceResponse,
) -> None:
    """注册流式与通用透传接口。"""

    @app.get(f"{API_PREFIX}/health")
    async def health(request: Request) -> JSONResponse:  # noqa: ANN202
        request_id = request.headers.get("x-request-id") or new_request_id()
        return JSONResponse(
            {"request_id": request_id, "ok": True, "data": {"status": "ready"}, "metadata": {}},
            headers={"X-Request-Id": request_id},
        )

    @app.get(f"{API_PREFIX}/events/stream")
    async def events_stream(request: Request) -> Any:  # noqa: ANN202
        """服务端主动推送的订阅通道（设计 §6，修 §2.5 的功能缺口）。

        WebSocket 传输下，``send_push`` 直接写当前 Gateway 连接；HTTP 下原本
        **没有任何通道**，定时任务提醒 / ``send_file_to_user`` / proactive 事件
        等推送全部静默丢失。客户端保持一条本连接即可收到。

        为什么是 GET：浏览器原生 ``EventSource`` 只支持 GET，前端可直接
        ``new EventSource('/api/v1/events/stream?session_id=...')``，
        无需手写 fetch-streaming 解析（对话响应仍是 POST + fetch）。

        ``session_id`` / ``channel_id`` 查询参数可选，用于**收窄**订阅范围；
        不传则接收全部推送。
        """
        from jiuwenswarm.server.transports.push_registry import get_push_registry
        from jiuwenswarm.server.transports.sink import STREAM_DONE, SSESink

        ctx = request_context(request)
        qp = request.query_params
        session_filter = qp.get("session_id") or ctx.session_id
        channel_filter = qp.get("channel_id")
        # 该头只表达 Gateway 是否支持反向 RPC，不再单独承担身份认证：
        # enforce 模式下本请求已先经过 mTLS 与 binding guard；off/observe 模式
        # 继续保留旧兼容语义，部署侧不得把本内部端点暴露给不受信任网络。
        reverse_rpc_capable = (
            request.headers.get("x-jiuwen-push-consumer") == "gateway"
        )

        registry = get_push_registry()
        sink = SSESink()
        # 订阅者 id 必须**每条连接唯一**，不能直接用 request_id ——
        # 后者取自客户端可控的 ``X-Request-Id``，链路追踪场景下多条 SSE 复用同一个值很常见。
        # 一旦重复：后注册的覆盖先注册的，先断开的那条又在 finally 里把后者摘掉，
        # 结果是「活着的连接永久收不到推送且零报错」（WS 单槽位语义已实测过这种杀伤力，
        # 见 push_registry 模块 docstring）。保留 request_id 前缀只为日志可关联。
        subscriber_id = f"http-sse:{ctx.request_id}:{uuid.uuid4().hex[:8]}"

        async def _events() -> Any:
            # 注册必须在生成器**体内**，与 finally 的注销成对 ——
            #
            # 放在体外看似等价，实则会漏：异步生成器若一次都没被迭代过就被关闭
            # （客户端在响应开始推送前即断开），按 PEP 525 语义**生成器体不执行**，
            # finally 自然也不执行，于是订阅者永久留在注册表里。它的队列无人消费，
            # 填满 maxsize 后，`PushRegistry.push` 每次扇到它都要等满超时才注销 ——
            # 进程级推送被一个早已消失的客户端拖累。
            registry.register(
                subscriber_id,
                sink,
                session_id=session_filter,
                channel_id=channel_filter,
                reverse_rpc_capable=reverse_rpc_capable,
            )
            try:
                if reverse_rpc_capable:
                    yield {
                        "event": "gateway.push_ready",
                        "data": json.dumps({"event_type": "gateway.push_ready"}),
                    }
                while True:
                    item = await sink.queue.get()
                    if item is STREAM_DONE:
                        break
                    yield {
                        "id": item.get("request_id") or ctx.request_id,
                        "event": _frame_event_name(item),
                        "data": json.dumps(item, ensure_ascii=False),
                    }
            finally:
                # 客户端断开 / 服务端收尾都要摘掉订阅，否则 registry 会持续堆积死连接。
                registry.unregister(subscriber_id)

        # sse_starlette 自带 ping（默认 15s 注释帧）保活，无需自行实现心跳。
        return response_type(_events(), headers={"X-Request-Id": ctx.request_id})

    async def _chat(request: Request, method: str) -> Any:
        params = await collect_params(request)
        ctx = request_context(request)
        session_id = ctx.session_id or params.get("session_id")
        if wants_stream(request, params):
            return response_type(
                server.iter_stream(
                    method,
                    params,
                    request_id=ctx.request_id,
                    session_id=session_id,
                    channel_id=ctx.channel_id,
                    user_id=ctx.user_id,
                    routing=ctx.routing,
                    tenant_ids=ctx.tenant_ids,
                    request_ext=ctx.request_ext,
                ),
                headers={"X-Request-Id": ctx.request_id},
            )
        payload, status = await server.invoke_unary(
            method,
            params,
            request_id=ctx.request_id,
            session_id=session_id,
            channel_id=ctx.channel_id,
            user_id=ctx.user_id,
            routing=ctx.routing,
            tenant_ids=ctx.tenant_ids,
            request_ext=ctx.request_ext,
        )
        return JSONResponse(
            payload, status_code=status, headers={"X-Request-Id": ctx.request_id}
        )

    @app.post(f"{API_PREFIX}/chat/completions")
    async def chat_completions(request: Request) -> Any:  # noqa: ANN202
        """流式（``Accept: text/event-stream``）或非流式发送消息。"""
        return await _chat(request, ReqMethod.CHAT_SEND.value)

    @app.post(f"{API_PREFIX}/chat/resume")
    async def chat_resume(request: Request) -> Any:  # noqa: ANN202
        return await _chat(request, ReqMethod.CHAT_RESUME.value)

    @app.get(f"{API_PREFIX}/sessions/{{session_id}}/history/stream")
    async def history_stream(request: Request) -> Any:  # noqa: ANN202
        params = await collect_params(request)
        ctx = request_context(request)
        return response_type(
            server.iter_stream(
                ReqMethod.HISTORY_GET.value,
                params,
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                channel_id=ctx.channel_id,
                user_id=ctx.user_id,
                routing=ctx.routing,
                tenant_ids=ctx.tenant_ids,
                request_ext=ctx.request_ext,
            ),
            headers={"X-Request-Id": ctx.request_id},
        )

    @app.post(f"{API_PREFIX}/rpc/{{method}}")
    async def generic_rpc(request: Request) -> Any:  # noqa: ANN202
        """通用透传：任意 ``ReqMethod`` 均可直接调用，覆盖未显式建模的方法。"""
        method = (request.path_params or {}).get("method", "")
        ctx = request_context(request)
        if not is_valid_req_method(method):
            return JSONResponse(
                {
                    "request_id": ctx.request_id,
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_METHOD",
                        "message": f"unknown req_method: {method}",
                        "details": {},
                    },
                },
                status_code=404,
            )
        params = await collect_params(request)
        params.pop("method", None)
        session_id = ctx.session_id or params.get("session_id")
        if wants_stream(request, params):
            return response_type(
                server.iter_stream(
                    method,
                    params,
                    request_id=ctx.request_id,
                    session_id=session_id,
                    channel_id=ctx.channel_id,
                    user_id=ctx.user_id,
                    routing=ctx.routing,
                    tenant_ids=ctx.tenant_ids,
                    request_ext=ctx.request_ext,
                ),
                headers={"X-Request-Id": ctx.request_id},
            )
        payload, status = await server.invoke_unary(
            method,
            params,
            request_id=ctx.request_id,
            session_id=session_id,
            channel_id=ctx.channel_id,
            user_id=ctx.user_id,
            routing=ctx.routing,
            tenant_ids=ctx.tenant_ids,
            request_ext=ctx.request_ext,
        )
        return JSONResponse(
            payload, status_code=status, headers={"X-Request-Id": ctx.request_id}
        )

    @app.post(f"{API_PREFIX}/e2a")
    async def e2a_passthrough(request: Request) -> Any:  # noqa: ANN202
        """协议直连：请求体为完整 E2A 信封，原样交给同一入口。"""
        raw_body = await request.body()
        request_id = request.headers.get("x-request-id") or new_request_id()
        try:
            envelope = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as exc:
            return JSONResponse(
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": str(exc), "details": {}},
                },
                status_code=400,
            )
        envelope.setdefault("request_id", request_id)
        if _envelope_wants_stream(request, envelope):
            # 信封自称流式：必须走 SSE。用非流式 sink 接会静默丢内容 ——
            # 业务层全程 send_chunk，而 UnaryHTTPSink 只记 send_unary/send_wire 的帧，
            # 结果是 HTTP 200 + data:null，调用方拿到"成功"却什么也没有。
            return response_type(
                server.iter_raw_envelope(
                    json.dumps(envelope, ensure_ascii=False), request_id=request_id
                ),
                headers={"X-Request-Id": request_id},
            )
        return await _invoke_raw_envelope(server, envelope, request_id)


async def _invoke_raw_envelope(
    server: AgentHTTPServer, envelope: dict[str, Any], request_id: str
) -> Any:
    """直接把 E2A 信封交给共享入口（非流式）。"""
    from jiuwenswarm.server.agent_http_server import frame_to_http_envelope
    from jiuwenswarm.server.transports.sink import UnaryHTTPSink

    sink = UnaryHTTPSink()
    try:
        await server.dispatch_raw_envelope(sink, json.dumps(envelope, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentHTTPRoutes] e2a 透传异常")
        return JSONResponse(
            {
                "request_id": request_id,
                "ok": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": {}},
            },
            status_code=500,
        )
    payload, status = frame_to_http_envelope(sink.last_frame, request_id)
    return JSONResponse(payload, status_code=status, headers={"X-Request-Id": request_id})
