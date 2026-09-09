# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Gateway Web HTTP mapped-route table (settings + workspace modules).

Each row is the contract: HTTP verb+path → WebChannel ``register_method`` name.
Handlers stay transport-agnostic; this module has no FastAPI / Gateway imports
so it can be unit-tested and used as the OpenAPI/catalog source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class WebHttpMappedRoute:
    """One HTTP surface over an existing WebChannel method."""

    http_method: str
    path: str
    rpc_method: str
    tag: str
    summary: str
    path_to_param: Mapping[str, str] = field(default_factory=dict)
    query_keys: tuple[str, ...] = ()
    accept_body: bool = False
    bind_session_param: bool = False
    created: bool = False
    extra_params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError(f"path must start with /: {self.path}")
        method = self.http_method.upper()
        object.__setattr__(self, "http_method", method)
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"unsupported HTTP method: {method}")
        if not str(self.rpc_method).strip():
            raise ValueError("rpc_method is required")


# Tenant ids accepted on query (and merged from X-Group-Id / X-Bot-Id / X-User-Id).
_TENANT_QUERY = ("group_id", "bot_id", "user_id", "service_id", "agent_id")
_SKILL_SESSION_QUERY = ("session_id",)
# Agent skills.evolution.* requires params.name (A1 / 企业浏览器契约).
_SKILL_NAME_QUERY = _SKILL_SESSION_QUERY + ("name",)
# Agent skills.skillnet.install_status requires params.install_id.
_SKILLNET_INSTALL_STATUS_QUERY = _SKILL_SESSION_QUERY + ("install_id",)
_CRON_QUERY = _TENANT_QUERY + ("project_id", "session_id", "count")

# ---------------------------------------------------------------------------
# settings — config / models / locale (enterprise read-mostly; writes via Manager)
# ---------------------------------------------------------------------------
_CONFIG_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/config", "config.get",
        "config", "读配置（企业只读；写权威在 Claw Manager）",
    ),
)

_A2A_INGRESS_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute("GET", "/a2a/ingress", "a2a.ingress.get", "a2a", "读取 A2A 入站服务状态"),
    WebHttpMappedRoute(
        "GET", "/a2a/ingress/history", "a2a.ingress.history", "a2a", "读取 A2A 入站请求处理历史",
        query_keys=("limit",),
    ),
    WebHttpMappedRoute(
        "PATCH", "/a2a/ingress", "a2a.ingress.update", "a2a", "保存 A2A 入站配置",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/a2a/ingress:enable", "a2a.ingress.enable", "a2a", "启用 A2A 入站服务",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/a2a/ingress:disable", "a2a.ingress.disable", "a2a", "停用 A2A 入站服务",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/a2a/ingress:reload", "a2a.ingress.reload", "a2a", "重载 A2A 入站服务",
        accept_body=True,
    ),
)

_A2A_OUTBOUND_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET",
        "/a2a/outbound/settings",
        "a2a.outbound.settings.get",
        "a2a",
        "读取 A2A 出站设置",
    ),
    WebHttpMappedRoute(
        "PATCH",
        "/a2a/outbound/settings",
        "a2a.outbound.settings.update",
        "a2a",
        "更新 A2A 出站设置",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST",
        "/a2a/outbound/discover",
        "a2a.outbound.discover",
        "a2a",
        "发现并预览第三方 A2A Agent",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST",
        "/a2a/outbound/agents",
        "a2a.outbound.register",
        "a2a",
        "显式注册第三方 A2A Agent",
        accept_body=True,
        created=True,
    ),
    WebHttpMappedRoute(
        "GET",
        "/a2a/outbound/agents",
        "a2a.outbound.list",
        "a2a",
        "列出已注册第三方 A2A Agent",
    ),
    WebHttpMappedRoute(
        "GET",
        "/a2a/outbound/agents/{agent_id}",
        "a2a.outbound.get",
        "a2a",
        "读取第三方 A2A Agent 注册项",
        path_to_param={"agent_id": "agent_id"},
    ),
    WebHttpMappedRoute(
        "PATCH",
        "/a2a/outbound/agents/{agent_id}/enabled",
        "a2a.outbound.enabled.update",
        "a2a",
        "更新 A2A Agent 的用户启用状态",
        path_to_param={"agent_id": "agent_id"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "PATCH",
        "/a2a/outbound/agents/{agent_id}",
        "a2a.outbound.update",
        "a2a",
        "更新第三方 A2A Agent 注册项",
        path_to_param={"agent_id": "agent_id"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST",
        "/a2a/outbound/agents/{agent_id}:refresh",
        "a2a.outbound.refresh",
        "a2a",
        "刷新第三方 A2A Agent Card",
        path_to_param={"agent_id": "agent_id"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST",
        "/a2a/outbound/agents/{agent_id}:confirm-revision",
        "a2a.outbound.confirm_revision",
        "a2a",
        "确认第三方 Agent Card 关键变化",
        path_to_param={"agent_id": "agent_id"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "DELETE",
        "/a2a/outbound/agents/{agent_id}",
        "a2a.outbound.delete",
        "a2a",
        "删除第三方 A2A Agent 注册项",
        path_to_param={"agent_id": "agent_id"},
    ),
    WebHttpMappedRoute(
        "GET",
        "/a2a/outbound/dispatches",
        "a2a.outbound.dispatch.list",
        "a2a",
        "列出 A2A 出站派发处理历史",
        query_keys=("limit",),
    ),
    WebHttpMappedRoute(
        "GET",
        "/a2a/outbound/dispatches/{dispatch_id}",
        "a2a.outbound.dispatch.get",
        "a2a",
        "读取 A2A 出站派发状态",
        path_to_param={"dispatch_id": "dispatch_id"},
    ),
)

_MODELS_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/models", "models.list",
        "models", "模型列表（企业聊天下拉只读）",
    ),
)

_LOCALE_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/locale", "locale.get_conf",
        "locale", "读 preferred_language",
    ),
    WebHttpMappedRoute(
        "PUT", "/locale", "locale.set_conf",
        "locale", "写 preferred_language（body.preferred_language=zh|en）",
        accept_body=True,
    ),
)

_CRON_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/cron/jobs", "cron.job.list",
        "cron", "列定时任务",
        query_keys=_CRON_QUERY,
    ),
    WebHttpMappedRoute(
        "POST", "/cron/jobs", "cron.job.create",
        "cron", "创建定时任务（body 字段与 RPC params 一致）",
        query_keys=_TENANT_QUERY,
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/cron/jobs/meta", "cron.job.meta",
        "cron", "Cron 元数据（modes/timeout 等）",
    ),
    WebHttpMappedRoute(
        "GET", "/cron/jobs/{id}", "cron.job.get",
        "cron", "获取单个任务",
        path_to_param={"id": "id"},
        query_keys=_CRON_QUERY,
    ),
    WebHttpMappedRoute(
        "PATCH", "/cron/jobs/{id}", "cron.job.update",
        "cron", "更新任务（body.patch 对象，与 RPC params 一致）",
        path_to_param={"id": "id"},
        query_keys=_TENANT_QUERY,
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "DELETE", "/cron/jobs/{id}", "cron.job.delete",
        "cron", "删除任务",
        path_to_param={"id": "id"},
        query_keys=_TENANT_QUERY,
    ),
    WebHttpMappedRoute(
        "POST", "/cron/jobs/{id}/actions/toggle", "cron.job.toggle",
        "cron", "启停任务（body.enabled）",
        path_to_param={"id": "id"},
        query_keys=_TENANT_QUERY,
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/cron/jobs/{id}/actions/preview", "cron.job.preview",
        "cron", "预览下次运行（可选 body/query count）",
        path_to_param={"id": "id"},
        query_keys=_CRON_QUERY,
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/cron/jobs/{id}/actions/run-now", "cron.job.run_now",
        "cron", "立即执行一次",
        path_to_param={"id": "id"},
        query_keys=_TENANT_QUERY,
        accept_body=True,
    ),
)

SETTINGS_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    *_CONFIG_ROUTES,
    *_A2A_INGRESS_ROUTES,
    *_A2A_OUTBOUND_ROUTES,
    *_MODELS_ROUTES,
    *_LOCALE_ROUTES,
    *_CRON_ROUTES,
)

# ---------------------------------------------------------------------------
# workspace — permissions.* (browser settings / avatar; not Claw Manager REST)
# ---------------------------------------------------------------------------
_PERMISSIONS_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/permissions/owner-scopes", "permissions.owner_scopes.get",
        "permissions", "数字分身 owner_scopes",
    ),
    WebHttpMappedRoute(
        "PUT", "/permissions/owner-scopes", "permissions.owner_scopes.set",
        "permissions", "写入 owner_scopes",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "GET", "/permissions/tools", "permissions.tools.get",
        "permissions", "工具级 allow/ask/deny",
    ),
    WebHttpMappedRoute(
        "PUT", "/permissions/tools", "permissions.tools.set",
        "permissions", "整表替换 permissions.tools",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/permissions/tools/actions/update", "permissions.tools.update",
        "permissions", "更新单条工具级别（body.tool + level；适合含特殊字符的工具名）",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "PATCH", "/permissions/tools/{tool}", "permissions.tools.update",
        "permissions", "更新单条工具级别",
        path_to_param={"tool": "tool"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "DELETE", "/permissions/tools/{tool}", "permissions.tools.delete",
        "permissions", "删除单条工具级别",
        path_to_param={"tool": "tool"},
    ),
    WebHttpMappedRoute(
        "GET", "/permissions/rules", "permissions.rules.get",
        "permissions", "列出参数级规则",
    ),
    WebHttpMappedRoute(
        "POST", "/permissions/rules", "permissions.rules.create",
        "permissions", "创建规则（body.rule）",
        accept_body=True,
        created=True,
    ),
    WebHttpMappedRoute(
        "PATCH", "/permissions/rules/{id}", "permissions.rules.update",
        "permissions", "更新规则（body.patch）",
        path_to_param={"id": "id"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "DELETE", "/permissions/rules/{id}", "permissions.rules.delete",
        "permissions", "删除规则",
        path_to_param={"id": "id"},
    ),
    WebHttpMappedRoute(
        "GET", "/permissions/approval-overrides", "permissions.approval_overrides.get",
        "permissions", "列出「总是允许」覆盖项",
    ),
    WebHttpMappedRoute(
        "DELETE", "/permissions/approval-overrides/{id}",
        "permissions.approval_overrides.delete",
        "permissions", "删除一条覆盖项",
        path_to_param={"id": "id"},
    ),
)

# ---------------------------------------------------------------------------
# workspace — skills.* (static prefixes before /skills/{name})
# ---------------------------------------------------------------------------
_SKILLS_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/skills", "skills.list",
        "skills", "技能列表（个人/本机目录）",
        query_keys=("with_installed", "refresh_marketplaces") + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/installed", "skills.installed",
        "skills", "已安装技能",
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/enterprise", "skills.enterprise.list",
        "skills", "企业租户已装技能（Gateway 本地表）",
        query_keys=_TENANT_QUERY,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/enterprise/actions/install", "skills.enterprise.install",
        "skills", "企业租户安装",
        query_keys=_TENANT_QUERY,
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/enterprise/actions/uninstall", "skills.enterprise.uninstall",
        "skills", "企业租户卸载",
        query_keys=_TENANT_QUERY,
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/marketplace", "skills.marketplace.list",
        "skills", "技能市场列表",
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/marketplace", "skills.marketplace.add",
        "skills", "添加市场源",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/marketplace/actions/remove", "skills.marketplace.remove",
        "skills", "移除市场源",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/marketplace/actions/toggle", "skills.marketplace.toggle",
        "skills", "启停市场源",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/clawhub/token", "skills.clawhub.get_token",
        "skills", "ClawHub token",
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "PUT", "/skills/clawhub/token", "skills.clawhub.set_token",
        "skills", "保存 ClawHub token",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/clawhub/search", "skills.clawhub.search",
        "skills", "搜索 ClawHub（query.q）",
        query_keys=("q", "limit") + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/clawhub/actions/search", "skills.clawhub.search",
        "skills", "搜索 ClawHub",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/clawhub/actions/download", "skills.clawhub.download",
        "skills", "从 ClawHub 安装",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/teamskillshub", "skills.teamskillshub.info",
        "skills", "TeamSkills Hub 信息",
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/teamskillshub/actions/init", "skills.teamskillshub.init",
        "skills", "TeamSkills Hub 初始化",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/teamskillshub/actions/validate", "skills.teamskillshub.validate",
        "skills", "校验待发布技能",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/teamskillshub/actions/pack", "skills.teamskillshub.pack",
        "skills", "打包技能",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/teamskillshub/actions/search", "skills.teamskillshub.search",
        "skills", "搜索 TeamSkills Hub",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/teamskillshub/actions/install", "skills.teamskillshub.install",
        "skills", "从 TeamSkills Hub 安装",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/teamskillshub/actions/publish", "skills.teamskillshub.publish",
        "skills", "发布到 TeamSkills Hub",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/teamskillshub/actions/delete", "skills.teamskillshub.delete",
        "skills", "删除 TeamSkills Hub 版本",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/sources", "skills.source.providers",
        "skills", "列出已配置技能源",
        query_keys=_TENANT_QUERY + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/sources/search", "skills.source.search",
        "skills", "搜索技能源",
        query_keys=("source_id", "q", "page", "page_size") + _TENANT_QUERY + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/sources/actions/search", "skills.source.search",
        "skills", "搜索技能源（含扩展筛选）",
        query_keys=_TENANT_QUERY,
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/sources/actions/install", "skills.source.install",
        "skills", "从技能源安装精确版本",
        query_keys=_TENANT_QUERY,
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/updates", "skills.updates.check",
        "skills", "检查已安装技能更新",
        query_keys=("source_id",) + _TENANT_QUERY + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/actions/update", "skills.update",
        "skills", "更新已安装技能",
        query_keys=_TENANT_QUERY,
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/enterprise/sources", "skills.enterprise.source.providers",
        "skills", "列出企业技能源",
        query_keys=_TENANT_QUERY + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/enterprise/sources/search", "skills.enterprise.source.search",
        "skills", "搜索企业技能源",
        query_keys=("source_id", "q", "page", "page_size") + _TENANT_QUERY + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/enterprise/sources/actions/search", "skills.enterprise.source.search",
        "skills", "搜索企业技能源（含扩展筛选）",
        query_keys=_TENANT_QUERY,
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/retrieval/status", "skills.retrieval.status",
        "skills", "技能检索索引状态",
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/retrieval/tree", "skills.retrieval.tree",
        "skills", "技能检索树",
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/retrieval/actions/index-build", "skills.retrieval.index_build",
        "skills", "构建检索索引",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/retrieval/actions/index-cancel", "skills.retrieval.index_cancel",
        "skills", "取消索引构建",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/retrieval/actions/search", "skills.retrieval.search",
        "skills", "检索技能",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/evolution/status", "skills.evolution.status",
        "skills", "技能进化状态（query: session_id, name）",
        query_keys=_SKILL_NAME_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/evolution", "skills.evolution.get",
        "skills", "当前进化配置（query: session_id, name）",
        query_keys=_SKILL_NAME_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "PUT", "/skills/evolution", "skills.evolution.save",
        "skills", "保存进化配置",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/evolution/archives", "skills.evolution.archives",
        "skills", "进化归档（query: session_id, name）",
        query_keys=_SKILL_NAME_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/evolution/actions/rollback", "skills.evolution.rollback",
        "skills", "进化回滚",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/evolution/actions/rebuild", "skills.evolution.rebuild",
        "skills", "重建进化产物",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/skillnet/actions/search", "skills.skillnet.search",
        "skills", "SkillNet 搜索",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/skillnet/actions/install", "skills.skillnet.install",
        "skills", "SkillNet 安装",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/skillnet/install-status", "skills.skillnet.install_status",
        "skills", "SkillNet 安装状态（query: session_id, install_id）",
        query_keys=_SKILLNET_INSTALL_STATUS_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/skillnet/actions/evaluate", "skills.skillnet.evaluate",
        "skills", "SkillNet 评估",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/online-search/actions/search", "skills.online_search.search",
        "skills", "在线搜索技能",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/actions/install", "skills.install",
        "skills", "安装技能（body.spec）",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/actions/uninstall", "skills.uninstall",
        "skills", "卸载技能（body.name）",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/actions/toggle", "skills.toggle",
        "skills", "启用/停用技能",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/skills/actions/import-local", "skills.import_local",
        "skills", "从本地路径导入",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/skills/{name}", "skills.get",
        "skills", "技能详情",
        path_to_param={"name": "name"},
        query_keys=("origin",) + _SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
)

# ---------------------------------------------------------------------------
# workspace — harness.* (packages on local workspace; Manager does not push these)
# ---------------------------------------------------------------------------
_HARNESS_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/harness/packages", "harness.packages",
        "harness", "列出 harness 包",
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/harness/packages/actions/scan", "harness.packages.scan",
        "harness", "扫描并刷新包列表",
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/harness/packages/actions/import", "harness.import",
        "harness", "导入 zip（JSON file_content=base64；multipart 见专用入口）",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/harness/packages/{package_id}/actions/activate", "harness.activate",
        "harness", "激活包",
        path_to_param={"package_id": "package_id"},
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "POST", "/harness/packages/{package_id}/actions/deactivate", "harness.deactivate",
        "harness", "停用包",
        path_to_param={"package_id": "package_id"},
        accept_body=True,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "GET", "/harness/packages/{package_id}/export", "harness.export",
        "harness", "导出包（返回 download_url / token，文件走既有 HTTP 下载）",
        path_to_param={"package_id": "package_id"},
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
    WebHttpMappedRoute(
        "DELETE", "/harness/packages/{package_id}", "harness.delete",
        "harness", "删除包",
        path_to_param={"package_id": "package_id"},
        query_keys=_SKILL_SESSION_QUERY,
        bind_session_param=True,
    ),
)

_PROJECT_QUERY = ("filter", "work_mode", "include_hidden", "limit", "cron_id")

_PROJECT_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    WebHttpMappedRoute(
        "GET", "/projects", "project.list",
        "projects", "列出项目（含默认项目与统计）",
        query_keys=_PROJECT_QUERY,
    ),
    WebHttpMappedRoute(
        "GET", "/projects/pinned-sessions", "project.pinned_sessions",
        "projects", "全部置顶会话",
    ),
    WebHttpMappedRoute(
        "GET", "/projects/{project_id}", "project.info",
        "projects", "项目详情（统计口径同 project.list）",
        path_to_param={"project_id": "project_id"},
    ),
    WebHttpMappedRoute(
        "GET", "/projects/{project_id}/sessions", "project.get_sessions",
        "projects", "项目下非置顶普通会话列表",
        path_to_param={"project_id": "project_id"},
        query_keys=("limit",),
    ),
    WebHttpMappedRoute(
        "GET", "/projects/{project_id}/cron-sessions", "project.get_cron_sessions",
        "projects", "项目下定时任务触发会话列表",
        path_to_param={"project_id": "project_id"},
        query_keys=("cron_id",),
    ),
    WebHttpMappedRoute(
        "POST", "/projects", "project.create",
        "projects", "创建项目（name/project_dir/work_mode 均可选）",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/projects/actions/restore", "project.restore",
        "projects", "恢复已软删除的项目（body.project_id）",
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "PATCH", "/projects/{project_id}", "project.rename",
        "projects", "重命名项目（body.name）",
        path_to_param={"project_id": "project_id"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "POST", "/projects/{project_id}/actions/pin", "project.pin",
        "projects", "置顶/取消置顶项目（body.pinned）",
        path_to_param={"project_id": "project_id"},
        accept_body=True,
    ),
    WebHttpMappedRoute(
        "DELETE", "/projects/{project_id}", "project.remove",
        "projects", "移除项目（软删除）",
        path_to_param={"project_id": "project_id"},
    ),
)

WORKSPACE_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    *_PERMISSIONS_ROUTES,
    *_PROJECT_ROUTES,
    *_SKILLS_ROUTES,
    *_HARNESS_ROUTES,
)

# All table-driven routes. Core chat/session routes stay hand-written in web_http_app.py.
MAPPED_ROUTES: tuple[WebHttpMappedRoute, ...] = (
    *SETTINGS_ROUTES,
    *WORKSPACE_ROUTES,
)

# Core routes implemented in web_http_app.py (hand-written because of SSE / history collect).
CORE_ROUTE_CATALOG: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/health", "", "探活（非 RPC method）"),
    ("GET", "/connection/status", "connection.status", "Agent 连接状态"),
    ("GET", "/sessions", "session.list", "列会话"),
    ("POST", "/sessions", "session.create", "建会话"),
    ("GET", "/sessions/{session_id}", "session.get_metadata", "会话元数据"),
    ("PATCH", "/sessions/{session_id}", "session.rename|session.pin", "重命名 / 置顶"),
    ("DELETE", "/sessions/{session_id}", "session.delete", "删除会话"),
    ("GET", "/sessions/{session_id}/history", "history.get", "JSON；Accept:text/event-stream 时 SSE"),
    ("POST", "/chat/completions", "chat.send", "发消息 SSE（主路径）"),
    ("POST", "/chat/resume", "chat.resume", "恢复中断对话"),
    ("POST", "/chat/{session_id}/actions/interrupt", "chat.interrupt", "中断生成"),
    ("POST", "/chat/{session_id}/actions/user_answer", "chat.user_answer", "回答 Agent 追问"),
)


def validate_mapped_routes(routes: Sequence[WebHttpMappedRoute] | None = None) -> None:
    """Fail fast on duplicate HTTP surfaces (path param names ignored)."""
    seen: set[tuple[str, str]] = set()
    for route in routes if routes is not None else MAPPED_ROUTES:
        key = (route.http_method, _normalize_path_key(route.path))
        if key in seen:
            raise ValueError(f"duplicate Web HTTP route {route.http_method} {route.path}")
        seen.add(key)


def _normalize_path_key(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part.startswith("{") and part.endswith("}"):
            parts.append("{}")
        else:
            parts.append(part)
    return "/".join(parts)


def _catalog_row_from_route(route: WebHttpMappedRoute, *, group: str) -> dict[str, Any]:
    return {
        "http_method": route.http_method,
        "path": f"/api/v1{route.path}",
        "rpc_method": route.rpc_method,
        "group": group,
        "tag": route.tag,
        "summary": route.summary,
        "bind_session_param": route.bind_session_param,
    }


def catalog_entries(
    *,
    include_core: bool = True,
    include_settings: bool = True,
    include_workspace: bool = True,
) -> list[dict[str, Any]]:
    """Machine-readable map for GET /api/v1/catalog and docs generation."""
    rows: list[dict[str, Any]] = []
    if include_core:
        for http_method, path, rpc_method, note in CORE_ROUTE_CATALOG:
            rows.append({
                "http_method": http_method,
                "path": f"/api/v1{path}",
                "rpc_method": rpc_method or None,
                "group": "core",
                "note": note,
            })
    if include_settings:
        for route in SETTINGS_ROUTES:
            rows.append(_catalog_row_from_route(route, group="settings"))
    if include_workspace:
        for route in WORKSPACE_ROUTES:
            rows.append(_catalog_row_from_route(route, group="workspace"))
    rows.append({
        "http_method": "POST",
        "path": "/api/v1/harness/packages/actions/import-file",
        "rpc_method": "harness.import",
        "group": "workspace",
        "tag": "harness",
        "summary": "multipart 字段 file → harness.import",
        "bind_session_param": False,
    })
    return rows


validate_mapped_routes()
