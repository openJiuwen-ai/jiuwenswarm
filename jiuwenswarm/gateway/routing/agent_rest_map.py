# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""E2A ``method`` → AgentServer REST（对照 ``docs/zh/Gateway-AgentServer-HTTP-REST组装.md``）。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urljoin

from jiuwenswarm.common.e2a.models import E2AEnvelope

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
DEFAULT_HTTP_PORT = 8766

_PATH_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Agent ``session.create`` 拒绝 params.session_id（恢复会话走 session.switch）。
# 与北向 ``bind_http_session`` / Web outbound 同约定：信封顶层 session_id 可进
# ``X-Session-Id``，但不得写入 REST body（路径也无 {session_id}）。
_METHODS_WITHOUT_PARAM_SESSION_ID = frozenset({"session.create"})

# method → (verb, path relative to /api/v1). 特殊 chat.* / history stream 在 assemble 里处理。
# 键同时覆盖本仓 ReqMethod 与 Agent HTTP ROUTES 若有字符串漂移的别名。
_ROUTE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("initialize", "POST", "/initialize"),
    ("session.list", "GET", "/sessions"),
    ("session.create", "POST", "/sessions"),
    ("session.rename", "PATCH", "/sessions/{session_id}"),
    ("session.delete", "DELETE", "/sessions/{session_id}"),
    ("session.switch", "POST", "/sessions/{session_id}/actions/switch"),
    ("session.fork", "POST", "/sessions/{session_id}/actions/fork"),
    ("session.rewind", "POST", "/sessions/{session_id}/actions/rewind"),
    ("session.rewind_and_restore", "POST", "/sessions/{session_id}/actions/rewind-restore"),
    ("session.rewind_compact", "POST", "/sessions/{session_id}/actions/rewind-compact"),
    ("session.rewind_context", "POST", "/sessions/{session_id}/actions/rewind-context"),
    ("session.restore_files", "POST", "/sessions/{session_id}/actions/restore-files"),
    ("history.get", "GET", "/sessions/{session_id}/history"),
    ("history.list_turns", "GET", "/sessions/{session_id}/turns"),
    ("chat.interrupt", "POST", "/chat/{session_id}/actions/interrupt"),
    ("chat.user_answer", "POST", "/chat/{session_id}/actions/answer"),
    ("command.session", "POST", "/sessions/{session_id}/commands"),
    ("command.compact", "POST", "/sessions/{session_id}/commands/compact"),
    ("command.compact_partial", "POST", "/sessions/{session_id}/commands/compact-partial"),
    ("command.model", "POST", "/sessions/{session_id}/commands/model"),
    ("command.mcp", "POST", "/sessions/{session_id}/commands/mcp"),
    ("command.sandbox", "POST", "/sessions/{session_id}/commands/sandbox"),
    ("command.btw", "POST", "/sessions/{session_id}/commands/btw"),
    ("command.add_dir", "POST", "/sessions/{session_id}/commands/add-dir"),
    ("command.chrome", "POST", "/sessions/{session_id}/commands/chrome"),
    ("command.context", "POST", "/sessions/{session_id}/commands/context"),
    ("command.recap", "POST", "/sessions/{session_id}/commands/recap"),
    ("command.diff", "POST", "/sessions/{session_id}/commands/diff"),
    ("command.simplify", "POST", "/sessions/{session_id}/commands/simplify"),
    ("command.resume", "POST", "/sessions/{session_id}/commands/resume"),
    ("command.workflows", "POST", "/sessions/{session_id}/commands/workflows"),
    ("command.goal", "POST", "/sessions/{session_id}/commands/goal"),
    ("command.status", "GET", "/sessions/{session_id}/commands/status"),
    ("agents.list", "GET", "/agents"),
    ("agents.create", "POST", "/agents"),
    ("agents.get", "GET", "/agents/{name}"),
    ("agents.update", "PUT", "/agents/{name}"),
    ("agents.delete", "DELETE", "/agents/{name}"),
    ("agents.enable", "POST", "/agents/{name}/actions/enable"),
    ("agents.disable", "POST", "/agents/{name}/actions/disable"),
    ("agents.tools.list", "GET", "/agents/{name}/tools"),
    ("agents.tools_list", "GET", "/agents/{name}/tools"),
    ("team.templates.list", "GET", "/teams/templates"),
    ("team.bindings.list", "GET", "/teams/bindings"),
    ("team.binding.create", "POST", "/teams/bindings"),
    ("team.binding.generate", "POST", "/teams/bindings/actions/generate"),
    ("team.session.bind", "POST", "/teams/{team_name}/sessions/{session_id}/bind"),
    ("team.snapshot", "GET", "/sessions/{session_id}/team/snapshot"),
    ("team.members.get", "GET", "/sessions/{session_id}/team/members"),
    ("team.history.get", "GET", "/sessions/{session_id}/team/history"),
    ("team.mq.publish", "POST", "/teams/mq/publish"),
    ("team.delete", "DELETE", "/teams/{team_name}"),
    ("skills.list", "GET", "/skills"),
    ("skills.installed", "GET", "/skills/installed"),
    ("skills.marketplace.list", "GET", "/skills/marketplace"),
    ("skills.marketplace.add", "POST", "/skills/marketplace"),
    ("skills.marketplace.remove", "DELETE", "/skills/marketplace/{name}"),
    ("skills.marketplace.toggle", "POST", "/skills/marketplace/{name}/actions/toggle"),
    ("skills.install", "POST", "/skills/install"),
    ("skills.import_local", "POST", "/skills/import-local"),
    ("skills.online.search", "POST", "/skills/online-search"),
    ("skills.online_search.search", "POST", "/skills/online-search"),
    ("skills.retrieval.status", "GET", "/skills/retrieval/status"),
    ("skills.retrieval.index_build", "POST", "/skills/retrieval/index-build"),
    ("skills.retrieval.index_cancel", "POST", "/skills/retrieval/index-cancel"),
    ("skills.retrieval.search", "GET", "/skills/retrieval/search"),
    ("skills.retrieval.tree", "GET", "/skills/retrieval/tree"),
    ("skills.evolution.get", "GET", "/skills/evolution"),
    ("skills.evolution.save", "PUT", "/skills/evolution"),
    ("skills.evolution.status", "GET", "/skills/{name}/evolution/status"),
    ("skills.evolution.archives", "GET", "/skills/evolution/archives"),
    ("skills.evolution.rollback", "POST", "/skills/evolution/actions/rollback"),
    ("skills.evolution.rebuild", "POST", "/skills/evolution/actions/rebuild"),
    ("skills.clawhub.get_token", "GET", "/skills/clawhub/token"),
    ("skills.clawhub.set_token", "PUT", "/skills/clawhub/token"),
    ("skills.clawhub.search", "GET", "/skills/clawhub/search"),
    ("skills.clawhub.download", "POST", "/skills/clawhub/actions/download"),
    ("skills.skillnet.search", "GET", "/skills/skillnet/search"),
    ("skills.skillnet.install", "POST", "/skills/skillnet/install"),
    ("skills.skillnet.install_status", "GET", "/skills/skillnet/install-status"),
    ("skills.skillnet.evaluate", "POST", "/skills/skillnet/actions/evaluate"),
    ("skills.teamskillshub.info", "GET", "/skills/teamskillshub/info"),
    ("skills.teamskillshub.search", "GET", "/skills/teamskillshub/search"),
    ("skills.teamskillshub.install", "POST", "/skills/teamskillshub/install"),
    ("skills.teamskillshub.init", "POST", "/skills/teamskillshub/actions/init"),
    ("skills.teamskillshub.validate", "POST", "/skills/teamskillshub/actions/validate"),
    ("skills.teamskillshub.pack", "POST", "/skills/teamskillshub/actions/pack"),
    ("skills.teamskillshub.publish", "POST", "/skills/teamskillshub/actions/publish"),
    ("skills.teamskillshub.delete", "POST", "/skills/teamskillshub/actions/delete"),
    ("skills.source.providers", "GET", "/skills/sources"),
    ("skills.source.search", "GET", "/skills/sources/search"),
    ("skills.source.install", "POST", "/skills/sources/install"),
    ("skills.updates.check", "GET", "/skills/updates"),
    ("skills.update", "POST", "/skills/actions/update"),
    ("skills.enterprise.list", "GET", "/skills/enterprise"),
    ("skills.enterprise.install", "POST", "/skills/enterprise/install"),
    ("skills.enterprise.uninstall", "POST", "/skills/enterprise/actions/uninstall"),
    ("skills.enterprise.source.providers", "GET", "/skills/enterprise/sources"),
    ("skills.enterprise.source.search", "GET", "/skills/enterprise/sources/search"),
    ("skills.get", "GET", "/skills/{name}"),
    ("skills.uninstall", "DELETE", "/skills/{name}"),
    ("skills.toggle", "POST", "/skills/{name}/actions/toggle"),
    ("extensions.list", "GET", "/extensions"),
    ("extensions.import", "POST", "/extensions"),
    ("extensions.delete", "DELETE", "/extensions/{name}"),
    ("extensions.toggle", "POST", "/extensions/{name}/actions/toggle"),
    ("hooks.list", "GET", "/hooks"),
    ("plugins.list", "GET", "/plugins"),
    ("plugins.install", "POST", "/plugins/install"),
    ("plugins.reload", "POST", "/plugins/actions/reload"),
    ("plugins.uninstall", "DELETE", "/plugins/{name}"),
    ("plugins.enable", "POST", "/plugins/{name}/actions/enable"),
    ("plugins.disable", "POST", "/plugins/{name}/actions/disable"),
    ("schedule.check_config", "GET", "/schedule/config"),
    ("schedule.update_config", "PATCH", "/schedule/config"),
    ("schedule.list", "GET", "/schedule/tasks"),
    ("schedule.create", "POST", "/schedule/tasks"),
    ("schedule.run", "POST", "/schedule/tasks/actions/run"),
    ("schedule.status", "GET", "/schedule/tasks/{task_id}"),
    ("schedule.logs", "GET", "/schedule/tasks/{task_id}/logs"),
    ("schedule.cancel", "POST", "/schedule/tasks/{task_id}/actions/cancel"),
    ("schedule.delete", "DELETE", "/schedule/tasks/{task_id}"),
    ("issue.watch_once", "POST", "/issues/actions/watch-once"),
    ("issue.state_list", "GET", "/issues/states"),
    ("issue.state.list", "GET", "/issues/states"),
    ("issue.matrix", "POST", "/issues/actions/matrix"),
    ("issue.delete", "DELETE", "/issues/{issue_id}"),
    ("harness.packages.get", "GET", "/harness/packages"),
    ("harness.packages.scan", "POST", "/harness/packages/scan"),
    ("harness.packages.activate", "POST", "/harness/packages/{name}/actions/activate"),
    ("harness.packages.deactivate", "POST", "/harness/packages/{name}/actions/deactivate"),
    ("harness.packages.delete", "DELETE", "/harness/packages/{name}"),
    ("permissions.enabled.get", "GET", "/permissions/enabled"),
    ("permissions.enabled.set", "PUT", "/permissions/enabled"),
    ("permissions.tools.get", "GET", "/permissions/tools"),
    ("permissions.tools.list", "GET", "/permissions/tools/list"),
    ("permissions.tools.set", "PUT", "/permissions/tools"),
    ("permissions.tools.update", "PATCH", "/permissions/tools"),
    ("permissions.tools.delete", "DELETE", "/permissions/tools/{rule_id}"),
    ("permissions.rules.get", "GET", "/permissions/rules"),
    ("permissions.rules.create", "POST", "/permissions/rules"),
    ("permissions.rules.update", "PATCH", "/permissions/rules/{rule_id}"),
    ("permissions.rules.delete", "DELETE", "/permissions/rules/{rule_id}"),
    ("permissions.approval_overrides.get", "GET", "/permissions/approval-overrides"),
    ("permissions.approval_overrides.delete", "DELETE", "/permissions/approval-overrides/{override_id}"),
    ("permissions.file_guard.workspace.rw_enabled.get", "GET", "/permissions/file-guard/workspace"),
    ("permissions.file_guard.workspace.rw_enabled.set", "PUT", "/permissions/file-guard/workspace"),
    ("permissions.file_guard.workspace.access.get", "GET", "/permissions/file-guard/workspace/access"),
    ("permissions.file_guard.workspace.access.set", "PUT", "/permissions/file-guard/workspace/access"),
    ("config.cache_clear", "POST", "/config/actions/cache-clear"),
    ("agent.reload_config", "POST", "/config/actions/agent-reload"),
    ("sync_agents_configs", "POST", "/config/actions/sync-agents"),
    ("agent.prewarm_sync", "POST", "/config/actions/prewarm-sync"),
    ("agent.prewarm.sync", "POST", "/config/actions/prewarm-sync"),
    ("browser.runtime_restart", "POST", "/runtime/browser/actions/restart"),
    ("proactive.tick", "POST", "/proactive/actions/tick"),
    ("acp.tool_response", "POST", "/acp/tool-responses"),
    ("symphony.plan", "POST", "/symphony/actions/plan"),
    ("symphony.build_score", "POST", "/symphony/actions/build-score"),
    ("symphony.pause_build", "POST", "/symphony/actions/pause-build"),
    ("symphony.score_status", "GET", "/symphony/score-status"),
    ("symphony.graph", "GET", "/symphony/graph"),
    ("channel.feishu.get_conf", "GET", "/channels/feishu/config"),
    ("channel.feishu.set_conf", "PUT", "/channels/feishu/config"),
    ("channel.xiaoyi.get_conf", "GET", "/channels/xiaoyi/config"),
    ("channel.xiaoyi.set_conf", "PUT", "/channels/xiaoyi/config"),
    ("channel.telegram.get_conf", "GET", "/channels/telegram/config"),
    ("channel.telegram.set_conf", "PUT", "/channels/telegram/config"),
    ("channel.slack.get_conf", "GET", "/channels/slack/config"),
    ("channel.slack.set_conf", "PUT", "/channels/slack/config"),
    ("channel.dingtalk.get_conf", "GET", "/channels/dingtalk/config"),
    ("channel.dingtalk.set_conf", "PUT", "/channels/dingtalk/config"),
    ("channel.whatsapp.get_conf", "GET", "/channels/whatsapp/config"),
    ("channel.whatsapp.set_conf", "PUT", "/channels/whatsapp/config"),
    ("channel.wechat.get_conf", "GET", "/channels/wechat/config"),
    ("channel.wechat.set_conf", "PUT", "/channels/wechat/config"),
    ("channel.wechat.get_login_ui", "GET", "/channels/wechat/login-ui"),
    ("channel.wechat.unbind", "POST", "/channels/wechat/actions/unbind"),
    ("updater.get_status", "GET", "/updater/status"),
    ("updater.check", "POST", "/updater/actions/check"),
    ("updater.download", "POST", "/updater/actions/download"),
    ("updater.get_conf", "GET", "/updater/config"),
    ("updater.set_conf", "PUT", "/updater/config"),
    ("heartbeat.get_conf", "GET", "/heartbeat/config"),
    ("heartbeat.set_conf", "PUT", "/heartbeat/config"),
)

REST_ROUTES: dict[str, tuple[str, str]] = {method: (verb, path) for method, verb, path in _ROUTE_ROWS}


class RestAssemblyError(ValueError):
    """REST 路径无法从信封拼出（缺 method / 缺路径占位符）。"""


@dataclass(frozen=True)
class AssembledRestRequest:
    verb: str
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None
    query: dict[str, str] | None
    used_rpc_fallback: bool


def normalize_agent_http_base(uri: str) -> str:
    """``http://host:8766`` 或已带 ``/api/v1`` → API 根（无末尾 /）。"""
    raw = (uri or "").strip().rstrip("/")
    if not raw:
        raise RestAssemblyError("AgentServer HTTP URL 为空")
    if raw.endswith(API_PREFIX):
        return raw
    return raw + API_PREFIX


def identity_headers(envelope: E2AEnvelope, *, accept: str) -> dict[str, str]:
    """组装 REST 身份头。

    REST body 只含业务 ``params``，不含整封 E2A；因此顶层 ``user_id`` 与
    ``channel_context.routing``（``group_id`` / ``bot_id`` / ``gateway_id``）
    必须经 ``X-*`` 头传到 Agent，由 Agent HTTP 入口重建。

    企业租户顶层字段同理：``service_id`` / ``agent_id`` / ``workspace_key``
    （Gateway ``apply_invoke_ids_to_envelope`` 写入）经
    ``X-Service-Id`` / ``X-Agent-Id`` / ``X-Workspace-Key`` 透传。

    ``channel_context.ext`` 是白名单过滤后的请求级扩展字段，经内部
    ``X-Jiuwenswarm-Request-Ext`` 传输；该头不承担鉴权。

    ``gateway_id`` 仅透传保留；Agent 业务（如企业配置 ``RoutingContext``）当前不消费。
    """
    from jiuwenswarm.common.request_ext import (
        INTERNAL_HEADER_NAME,
        METADATA_KEY,
        encode_internal_header,
    )
    from jiuwenswarm.common.request_identity import web_routing_identity

    headers = {
        "X-Request-Id": str(envelope.request_id or ""),
        "X-Channel-Id": str(envelope.channel or "web"),
        "Accept": accept,
    }
    session_id = envelope.session_id or (envelope.params or {}).get("session_id")
    if session_id:
        headers["X-Session-Id"] = str(session_id)
    identity = web_routing_identity(
        envelope.channel_context if isinstance(envelope.channel_context, dict) else None
    )
    user_id = envelope.user_id or identity.get("user_id")
    if user_id:
        headers["X-User-Id"] = str(user_id)
    for field, header_name in (
        ("group_id", "X-Group-Id"),
        ("bot_id", "X-Bot-Id"),
        ("gateway_id", "X-Gateway-Id"),
    ):
        value = identity.get(field)
        if value:
            headers[header_name] = value
    for attr, header_name in (
        ("service_id", "X-Service-Id"),
        ("agent_id", "X-Agent-Id"),
        ("workspace_key", "X-Workspace-Key"),
    ):
        value = getattr(envelope, attr, None)
        text = str(value).strip() if value is not None else ""
        if text:
            headers[header_name] = text
    channel_context = (
        envelope.channel_context if isinstance(envelope.channel_context, dict) else {}
    )
    encoded_ext = encode_internal_header(channel_context.get(METADATA_KEY))
    if encoded_ext:
        headers[INTERNAL_HEADER_NAME] = encoded_ext
    return headers


def _lookup_route(method: str, *, is_stream: bool) -> tuple[str, str, bool]:
    if method in {"chat.send", "chat.resume"}:
        path = "/chat/completions" if method == "chat.send" else "/chat/resume"
        return "POST", path, False
    if method == "history.get" and is_stream:
        return "GET", "/sessions/{session_id}/history/stream", False
    found = REST_ROUTES.get(method)
    if found is not None:
        return found[0], found[1], False
    logger.warning("[AgentRestMap] method=%s 不在 REST 表，降级 POST /rpc/{method}", method)
    return "POST", f"/rpc/{quote(method, safe='.')}", True


def _fill_path(template: str, values: Mapping[str, Any]) -> tuple[str, set[str]]:
    used: set[str] = set()
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values or values[key] is None or values[key] == "":
            missing.append(key)
            return match.group(0)
        used.add(key)
        return quote(str(values[key]), safe="")

    filled = _PATH_PLACEHOLDER.sub(_replace, template)
    if missing:
        raise RestAssemblyError(f"REST 路径缺占位符 {missing}: {template}")
    return filled, used


def _remaining_params(params: Mapping[str, Any], used: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in used or value is None:
            continue
        out[key] = value
    return out


def _query_values(remaining: Mapping[str, Any]) -> dict[str, str]:
    query: dict[str, str] = {}
    for key, value in remaining.items():
        if isinstance(value, (dict, list)):
            query[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            query[key] = str(value)
    return query


def assemble_rest_request(envelope: E2AEnvelope, *, base_url: str) -> AssembledRestRequest:
    """信封 → REST/SSE；body 保持纯 params，上下文通过内部 header 映射。"""
    method = str(envelope.method or "").strip()
    if not method:
        raise RestAssemblyError("envelope.method 为空，无法组装 REST")

    verb, template, rpc = _lookup_route(method, is_stream=bool(envelope.is_stream))
    values: dict[str, Any] = dict(envelope.params or {})
    if method in _METHODS_WITHOUT_PARAM_SESSION_ID:
        # 勿 setdefault 信封 session_id；并剥掉 params 里误带的客户端临时 id。
        values.pop("session_id", None)
    elif envelope.session_id and not values.get("session_id"):
        values["session_id"] = envelope.session_id

    filled, used = _fill_path(template, values)
    remaining = _remaining_params(values, used)
    api_root = normalize_agent_http_base(base_url)
    url = urljoin(api_root + "/", filled.lstrip("/"))

    accept = "text/event-stream" if envelope.is_stream else "application/json"
    headers = identity_headers(envelope, accept=accept)
    json_body: dict[str, Any] | None = None
    query: dict[str, str] | None = None
    if verb == "GET" or template.endswith("/history/stream"):
        query = _query_values(remaining) or None
    else:
        headers["Content-Type"] = "application/json"
        json_body = remaining
    return AssembledRestRequest(
        verb=verb,
        url=url,
        headers=headers,
        json_body=json_body,
        query=query,
        used_rpc_fallback=rpc,
    )
