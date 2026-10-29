# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight allowlists for facade management requests."""

from jiuwenswarm.common.schema.message import ReqMethod

_SKILLDEV_METHODS: frozenset[ReqMethod] = frozenset(
    m for m in ReqMethod if m.value.startswith("skilldev.")
)

_SKILL_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.SKILLS_LIST: "handle_skills_list",
    ReqMethod.SKILLS_INSTALLED: "handle_skills_installed",
    ReqMethod.SKILLS_GET: "handle_skills_get",
    ReqMethod.SKILLS_VERSIONS_LIST: "handle_skills_versions_list",
    ReqMethod.SKILLS_FILES_LIST: "handle_skills_files_list",
    ReqMethod.SKILLS_FILES_GET: "handle_skills_files_get",
    ReqMethod.SKILLS_REBUILD: "handle_skills_rebuild",
    ReqMethod.SKILLS_TOGGLE: "handle_skills_toggle",
    ReqMethod.SKILLS_MARKETPLACE_LIST: "handle_skills_marketplace_list",
    ReqMethod.SKILLS_INSTALL: "handle_skills_install",
    ReqMethod.SKILLS_PACK_MEMBER_INSTALL: "handle_skills_pack_member_install",
    ReqMethod.SKILLS_UNINSTALL: "handle_skills_uninstall",
    ReqMethod.SKILLS_IMPORT_LOCAL: "handle_skills_import_local",
    ReqMethod.SKILLS_IMPORT_UPLOAD: "handle_skills_import_upload",
    ReqMethod.SKILLS_CREATE_FROM_KNOWLEDGE: "handle_skills_create_from_knowledge",
    ReqMethod.SKILLS_MARKETPLACE_ADD: "handle_skills_marketplace_add",
    ReqMethod.SKILLS_MARKETPLACE_REMOVE: "handle_skills_marketplace_remove",
    ReqMethod.SKILLS_MARKETPLACE_TOGGLE: "handle_skills_marketplace_toggle",
    ReqMethod.SKILLS_ONLINE_SEARCH: "handle_skills_online_search",
    ReqMethod.SKILLS_ONLINE_SEARCH_INSTALL: "handle_skills_online_search_install",
    ReqMethod.SKILLS_SKILLNET_SEARCH: "handle_skills_skillnet_search",
    ReqMethod.SKILLS_SKILLNET_INSTALL: "handle_skills_skillnet_install",
    ReqMethod.SKILLS_SKILLNET_INSTALL_STATUS: "handle_skills_skillnet_install_status",
    ReqMethod.SKILLS_SKILLNET_EVALUATE: "handle_skills_skillnet_evaluate",
    ReqMethod.SKILLS_CLAWHUB_GET_TOKEN: "handle_skills_clawhub_get_token",
    ReqMethod.SKILLS_CLAWHUB_SET_TOKEN: "handle_skills_clawhub_set_token",
    ReqMethod.SKILLS_CLAWHUB_SEARCH: "handle_skills_clawhub_search",
    ReqMethod.SKILLS_CLAWHUB_DOWNLOAD: "handle_skills_clawhub_download",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INFO: "handle_skills_team_skills_hub_info",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INIT: "handle_skills_team_skills_hub_init",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_VALIDATE: "handle_skills_team_skills_hub_validate",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_PACK: "handle_skills_team_skills_hub_pack",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_SEARCH: "handle_skills_team_skills_hub_search",
    ReqMethod.SKILLS_SWARMSKILLS_HUB_RECOMMEND: "handle_skills_swarm_skills_hub_recommend",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INSTALL: "handle_skills_team_skills_hub_install",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_PUBLISH: "handle_skills_team_skills_hub_publish",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_DELETE: "handle_skills_team_skills_hub_delete",
    ReqMethod.SKILLS_SWARMSKILLS_HUB_DETAIL: "handle_skills_swarm_skills_hub_detail",
    ReqMethod.SKILLS_RETRIEVAL_STATUS: "handle_skills_retrieval_status",
    ReqMethod.SKILLS_RETRIEVAL_INDEX_BUILD: "handle_skills_retrieval_index_build",
    ReqMethod.SKILLS_RETRIEVAL_INDEX_CANCEL: "handle_skills_retrieval_index_cancel",
    ReqMethod.SKILLS_RETRIEVAL_SEARCH: "handle_skills_retrieval_search",
    ReqMethod.SKILLS_RETRIEVAL_TREE: "handle_skills_retrieval_tree",
    ReqMethod.SKILLS_GRAPH_BUILD: "handle_skills_graph_build",
    ReqMethod.SKILLS_GRAPH_STATUS: "handle_skills_graph_status",
    ReqMethod.SKILLS_GRAPH_GET: "handle_skills_graph_get",
    ReqMethod.SKILLS_GRAPH_CANCEL: "handle_skills_graph_cancel",
    ReqMethod.SKILLS_EXPERIENCE_LIST: "handle_skills_experience_list",
    ReqMethod.SKILLS_EXPERIENCE_REQUEST: "handle_skills_experience_request",
    ReqMethod.SKILLS_EVOLUTION_STATUS: "handle_skills_evolution_status",
    ReqMethod.SKILLS_EVOLUTION_GET: "handle_skills_evolution_get",
    ReqMethod.SKILLS_EVOLUTION_SAVE: "handle_skills_evolution_save",
    ReqMethod.SKILLS_VISIBILITY_GET: "handle_skills_visibility_get",
    ReqMethod.SKILLS_VISIBILITY_SET: "handle_skills_visibility_set",
    ReqMethod.SKILLS_VISIBILITY_UPDATE: "handle_skills_visibility_update",
    # skills.sync.* 仅保留 reload：web 进程 HTTP apply 落盘后的进程间通知，
    # 不落盘、只触发重建。diff / package / apply 不进 WS 路由——它们的
    # token 鉴权只在 HTTP 层（/skill-sync/*），经 WS 调用会绕过鉴权；
    # 且 WS package 无人清理临时目录、WS apply 的 zip_bytes 无法过 JSON
    # 序列化，均不可用。外部统一走 HTTP，进程内测试直调 SkillManager。
    ReqMethod.SKILL_SYNC_RELOAD: "handle_skill_sync_reload",
}

_SESSION_ASSET_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.SESSION_ASSETS_LIST: "handle_session_assets_list",
    ReqMethod.SESSION_ASSETS_REGISTER: "handle_session_assets_register",
    ReqMethod.SESSION_ASSETS_RENAME: "handle_session_assets_rename",
    ReqMethod.SESSION_ASSETS_REMOVE: "handle_session_assets_remove",
}

# Handlers that persist a Skill visibility document; every one of them must
# trigger a rail refresh so a grant or a revocation takes effect on the next
# turn. The read-only ``get`` deliberately stays out.
_SKILL_VISIBILITY_WRITE_HANDLERS: frozenset[str] = frozenset(
    {
        "handle_skills_visibility_set",
        "handle_skills_visibility_update",
    }
)

# 同步 handler 涉及技能目录与 local_skills，处理前须重载磁盘 state，
# 保证 AgentServer 长生命周期 _skill_manager 实例看到最新状态
# （handle_skill_sync_reload：web 进程刚落盘，state 必须重读）。
_SKILL_SYNC_HANDLERS: frozenset[str] = frozenset(
    {
        "handle_skill_sync_reload",
    }
)

# 已收缩出 WS 路由的同步方法：合法枚举值不会被入口解析拦截，须在
# _handle_skills_request 显式拒绝，否则落入聊天路径触发空 query 的
# LLM 调用。
_SKILL_SYNC_WS_REJECTED: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.SKILL_SYNC_DIFF,
        ReqMethod.SKILL_SYNC_PACKAGE,
        ReqMethod.SKILL_SYNC_APPLY,
    }
)

_PLUGIN_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.PLUGINS_LIST: "handle_plugins_list",
    ReqMethod.PLUGINS_INSTALL: "handle_plugins_install",
    ReqMethod.PLUGINS_UNINSTALL: "handle_plugins_uninstall",
    ReqMethod.PLUGINS_ENABLE: "handle_plugins_enable",
    ReqMethod.PLUGINS_DISABLE: "handle_plugins_disable",
    ReqMethod.PLUGINS_RELOAD: "handle_plugins_reload",
}

# Catalog + lifecycle: method → package_manager callable.
_PACKAGE_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.AGENT_GROUPS_LIST: "list_agent_groups",
    ReqMethod.AGENT_GROUPS_SHOW: "show_agent_group",
    ReqMethod.AGENT_GROUPS_FILE_LIST: "list_agent_group_files",
    ReqMethod.AGENT_GROUPS_FILE_READ: "read_agent_group_file",
    ReqMethod.AGENT_GROUPS_CREATE: "create_agent_group",
    ReqMethod.AGENT_GROUPS_IMPORT_LOCAL: "import_agent_group",
    ReqMethod.AGENT_GROUPS_INSTALL: "install_agent_group",
    ReqMethod.AGENT_GROUPS_UNINSTALL: "uninstall_agent_group",
    ReqMethod.AGENT_TEMPLATES_LIST: "list_agent_templates",
    ReqMethod.AGENT_TEMPLATES_SHOW: "show_agent_template",
    ReqMethod.AGENT_TEMPLATES_FILE_LIST: "list_agent_template_files",
    ReqMethod.AGENT_TEMPLATES_FILE_READ: "read_agent_template_file",
    ReqMethod.AGENT_TEMPLATES_CREATE: "create_agent_template",
    ReqMethod.AGENT_TEMPLATES_UPDATE: "update_agent_template",
    ReqMethod.AGENT_TEMPLATES_DELETE: "delete_agent_template",
    ReqMethod.AGENT_TEMPLATES_IMPORT_LOCAL: "import_agent_template",
    ReqMethod.AGENT_TEMPLATES_INSTALL: "install_agent_template",
    ReqMethod.AGENT_TEMPLATES_UNINSTALL: "uninstall_agent_template",
    ReqMethod.PLUGIN_PACKAGES_LIST: "list_plugin_packages",
    ReqMethod.PLUGIN_PACKAGES_SHOW: "show_plugin_package",
    ReqMethod.PLUGIN_PACKAGES_CREATE: "create_plugin_package",
    ReqMethod.PLUGIN_PACKAGES_IMPORT_LOCAL: "import_plugin_package",
    ReqMethod.PLUGIN_PACKAGES_INSTALL: "install_plugin_package",
    ReqMethod.PLUGIN_PACKAGES_UNINSTALL: "uninstall_plugin_package",
}

_AGENT_GROUP_PACKAGE_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.AGENT_GROUPS_LIST,
        ReqMethod.AGENT_GROUPS_SHOW,
        ReqMethod.AGENT_GROUPS_FILE_LIST,
        ReqMethod.AGENT_GROUPS_FILE_READ,
        ReqMethod.AGENT_GROUPS_CREATE,
        ReqMethod.AGENT_GROUPS_IMPORT_LOCAL,
        ReqMethod.AGENT_GROUPS_INSTALL,
        ReqMethod.AGENT_GROUPS_UNINSTALL,
    }
)
