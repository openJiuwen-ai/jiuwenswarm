# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``openjiuwen_protocol.message``。

仅保留 ``jiuwenswarm.common.schema.message`` 旧 import 路径兼容，re-export 协议包
的同一对象——``Message`` / ``ReqMethod`` / ``EventType`` / ``Mode`` 及线协议事件名
常量。``ReqMethod`` 的成员扩展（``RECOMMENDATION`` / ``TRAJECTORY_*``）现由协议包
直接提供；仅协议包当前 pin 尚未发布的成员（``SESSION_ASSETS_REMOVE``）保留最小
运行时补齐，避免两仓各维护一份枚举类。
"""

from __future__ import annotations

from enum import Enum

from openjiuwen_protocol.message import (  # noqa: F401
    PROMPT_SUGGESTION_READY_EVENT,
    SESSION_REQUEST_COMPLETED_EVENT,
    TEAM_ATTACH_ONLY_PARAM,
    TOPIC_STARTERS_CONTROL_CHANGED_EVENT,
    TOPIC_STARTERS_READY_EVENT,
    EventType,
    Message,
    Mode,
    ReqMethod,
)


def _ensure_protocol_member(enum_cls: type[Enum], name: str, value: str) -> None:
    """Register a member of a protocol enum the pinned protocol package does not ship yet.

<<<<<<< HEAD
    CHAT_SEND = "chat.send"
    CHAT_RESUME = "chat.resume"
    CHAT_CANCEL = "chat.interrupt"
    CHAT_ANSWER = "chat.user_answer"
    CHAT_SWARMFLOW_REPLY = "chat.swarmflow_reply"
    SSH_RELAY = "ssh.relay"
    HISTORY_GET = "history.get"
    GROUP_CREATE = "group.create"
    GROUP_DELETE = "group.delete"
    GROUP_LIST = "group.list"
    GROUP_GET = "group.get"
    GROUP_UPDATE = "group.update"
    GROUP_MEMBER_ADD = "group.member.add"
    GROUP_HISTORY_GET = "group.history.get"
    COMMAND_BTW = "command.btw"
    COMMAND_ADD_DIR = "command.add_dir"
    COMMAND_CHROME = "command.chrome"
    COMMAND_COMPACT = "command.compact"
    COMMAND_COMPACT_PARTIAL = "command.compact_partial"
    COMMAND_CONTEXT = "command.context"
    COMMAND_RECAP = "command.recap"
    COMMAND_DIFF = "command.diff"
    COMMAND_SIMPLIFY = "command.simplify"
    COMMAND_MCP = "command.mcp"
    COMMAND_MODEL = "command.model"
    COMMAND_RESUME = "command.resume"
    COMMAND_SANDBOX = "command.sandbox"
    COMMAND_SESSION = "command.session"
    COMMAND_WORKFLOWS = "command.workflows"
    COMMAND_STATUS = "command.status"
    SWARMFLOW_PAUSE = "swarmflow.pause"
    SWARMFLOW_RESUME = "swarmflow.resume"
    SWARMFLOW_STOP = "swarmflow.stop"

    CONFIG_GET = "config.get"
    CONFIG_MOA_STATUS = "config.moa_status"
    CONFIG_SET = "config.set"
    CONFIG_SAVE_ALL = "config.save_all"
    CONFIG_VALIDATE_MODEL = "config.validate_model"
    MODELS_LIST = "models.list"
    MODELS_ZEN_ENTRIES = "models.zen_entries"
    MODELS_ZEN_WARM = "models.zen_warm"
    MODELS_GET = "models.get"
    MODELS_REPLACE_ALL = "models.replace_all"
    MODELS_VALIDATE = "models.validate"
    MODELS_UPSERT = "models.upsert"
    MODELS_DELETE = "models.delete"
    MODELS_REFERENCES = "models.references"
    MODEL_GROUPS_LIST = "model_groups.list"
    MODEL_GROUPS_UPSERT = "model_groups.upsert"
    MODEL_GROUPS_DELETE = "model_groups.delete"
    SESSION_SELECTION_SET = "session.selection.set"
    LOCALE_GET_CONF = "locale.get_conf"
    LOCALE_SET_CONF = "locale.set_conf"
    CHANNEL_GET = "channel.get"

    SESSION_LIST = "session.list"
    SESSION_GET_METADATA = "session.get_metadata"
    SESSION_MESSAGE_CONTINUE_QUEUED = "session.message.continue_queued"
    SESSION_PLAN_STATUS = "session.plan_status"
    SESSION_PIN = "session.pin"
    SESSION_COLOR_SET = "session.color_set"
    SESSION_PREVIEW = "session.preview"
    SESSION_CREATE = "session.create"
    SESSION_SWITCH = "session.switch"
    SESSION_DETACH = "session.detach"
    SESSION_DELETE = "session.delete"
    SESSION_INPUT_INTENT = "session.input.intent"
    SESSION_RENAME = "session.rename"
    SESSION_FORK = "session.fork"
    SESSION_REBIND_PROJECT = "session.rebind_project"
    SESSION_REWIND = "session.rewind"
    SESSION_REWIND_AND_RESTORE = "session.rewind_and_restore"
    SESSION_REWIND_CONTEXT = "session.rewind_context"
    SESSION_REWIND_COMPACT = "session.rewind_compact"
    SESSION_RESTORE_FILES = "session.restore_files"
    HISTORY_LIST_TURNS = "history.list_turns"
    HISTORY_APPEND_RECORD = "history.append_record"
    TEAM_TEMPLATES_LIST = "team.templates.list"
    TEAM_BINDINGS_LIST = "team.bindings.list"
    TEAM_BINDING_CREATE = "team.binding.create"
    TEAM_BINDING_GENERATE = "team.binding.generate"
    TEAM_SESSION_BIND = "team.session.bind"
    TEAM_DELETE = "team.delete"

    PATH_GET = "path.get"
    PATH_SET = "path.set"
    PATH_SELECT_DIRECTORY = "path.select_directory"
    PATH_SELECT_FILES = "path.select_files"

    BROWSER_RUNTIME_RESTART = "browser.runtime_restart"

    CONFIG_CACHE_CLEAR = "config.cache_clear"
    AGENT_RELOAD_CONFIG = "agent.reload_config"
    AGENT_PREWARM_SYNC = "agent.prewarm.sync"
    # Gateway → AgentServer：登录模型凭据续期后的新 token / 会话注销后的撤销
    AUTH_CREDENTIALS_UPDATE = "auth.credentials.update"

    MEMORY_COMPUTE = "memory.compute"
    # TUI memory management (Phase 3: execute in the target AgentServer's
    # injected user directory; Gateway only forwards the request).
    MEMORY_LIST = "memory.list"
    MEMORY_EDIT = "memory.edit"
    MEMORY_STATUS = "memory.status"
    MEMORY_TOGGLE = "memory.toggle"
    MEMORY_OPEN = "memory.open"

    # Project domain (Phase 3).  The Gateway routes these calls but all
    # project-store and session-metadata access happens in AgentServer.
    PROJECT_INFO = "project.info"
    PROJECT_PINNED_SESSIONS = "project.pinned_sessions"
    PROJECT_GET_SESSIONS = "project.get_sessions"
    PROJECT_GET_CRON_SESSIONS = "project.get_cron_sessions"
    # Resolve a cron project's binding in the selected AgentServer directory.
    # Gateway keeps the job store/scheduler but must not inspect user projects.
    PROJECT_CRON_RESOLVE_BINDING = "project.cron.resolve_binding"
    PROJECT_LIST = "project.list"
    PROJECT_CREATE = "project.create"
    PROJECT_RENAME = "project.rename"
    PROJECT_PIN = "project.pin"
    SESSION_ARCHIVE = "session.archive"
    SESSION_UNARCHIVE = "session.unarchive"
    SESSION_ARCHIVED_LIST = "session.archived.list"
    CRON_SESSIONS_DELETE = "cron.sessions.delete"
    PROJECT_REMOVE = "project.remove"
    PROJECT_RESTORE = "project.restore"
    PROJECT_LIFECYCLE = "project.lifecycle"
    PROJECT_SESSIONS_ARCHIVE = "project.sessions.archive"
    PROJECT_SESSIONS_DELETE_ARCHIVED = "project.sessions.delete_archived"
    PROJECT_GIT_STATUS = "project.git.status"
    PROJECT_GIT_PROBE = "project.git.probe"
    PROJECT_GIT_INIT = "project.git.init"
    PROJECT_GIT_SWITCH_BRANCH = "project.git.switch_branch"
    PROJECT_GIT_CREATE_BRANCH = "project.git.create_branch"
    PROJECT_GIT_COMMIT = "project.git.commit"
    PROJECT_GIT_PUSH = "project.git.push"
    PROJECT_GIT_DIFF_STATUS = "project.git.diff_status"
    PROJECT_GIT_TURN_DIFF_LIST = "project.git.turn_diff_list"
    PROJECT_GIT_TURN_DIFF = "project.git.turn_diff"
    PROJECT_GIT_DISCARD_TURN_CHANGES = "project.git.discard_turn_changes"
    PROJECT_GIT_REDO_TURN_CHANGES = "project.git.redo_turn_changes"

    # Designer execution graph (canvas + subagent orchestration shell)
    DESIGNER_WORKSPACE_CREATE = "designer.workspace.create"
    DESIGNER_WORKSPACE_GET = "designer.workspace.get"
    DESIGNER_WORKSPACE_SESSION_CREATE = "designer.workspace.session.create"
    DESIGNER_WORKSPACE_SESSION_COMPOSE = "designer.workspace.session.compose"
    DESIGNER_GRAPH_GET = "designer.graph.get"
    DESIGNER_GRAPH_LIST = "designer.graph.list"
    DESIGNER_GRAPH_SAVE = "designer.graph.save"
    DESIGNER_GRAPH_BOOTSTRAP = "designer.graph.bootstrap"
    DESIGNER_GRAPH_PATCH = "designer.graph.patch"
    DESIGNER_GRAPH_CHAT = "designer.graph.chat"
    DESIGNER_MEDIA_CAPABILITIES = "designer.media.capabilities"
    DESIGNER_RUN_START = "designer.run.start"
    DESIGNER_RUN_GET = "designer.run.get"
    DESIGNER_RUN_PAUSE = "designer.run.pause"
    DESIGNER_RUN_CANCEL = "designer.run.cancel"

    # WebUI 扩展：上行事件统一入口，事件名在 params.event。
    WEBUI_DISPATCH = "webui.dispatch"
    WEBUI_SETTINGS_CATALOG = "webui.settings.catalog"
    WEBUI_RESOURCE_READ = "webui.resource.read"
    # Internal Gateway lifecycle notification, not a browser request method.
    WEBUI_CONNECTION_CLOSED = "webui.connection.closed"

    EXTENSIONS_V2_QUERY = "extensions.v2.query"
    EXTENSIONS_V2_CATALOG = "extensions.v2.catalog"
    EXTENSIONS_V2_SHOW = "extensions.v2.show"
    EXTENSIONS_V2_IMPORT = "extensions.v2.import"
    EXTENSIONS_V2_INSTALL = "extensions.v2.install"
    EXTENSIONS_V2_UNINSTALL = "extensions.v2.uninstall"
    EXTENSIONS_V2_RELOAD = "extensions.v2.reload"
    EXTENSIONS_V2_SET_ENABLED = "extensions.v2.set_enabled"
    EXTENSIONS_V2_FILE_LIST = "extensions.v2.file.list"
    EXTENSIONS_V2_FILE_READ = "extensions.v2.file.read"

    PROACTIVE_TICK = "proactive.tick"  # Trigger proactive recommendation tick (from Cron)
    PROACTIVE_FEEDBACK = "proactive.feedback"  # User feedback on proactive recommendation (like/dislike)
    COMMAND_GOAL = "command.goal"
    COMMANDS_LIST = "commands.list"

    FILES_LIST = "files.list"
    FILES_GET = "files.get"

    # 媒体/文档附件（Phase 2 WorkspaceFileAdapter）
    MEDIA_PERSIST = "media.persist"
    # 丢弃尚未发送的会话 uploads 副本。只删当前会话 uploads 内的普通文件。
    MEDIA_DISCARD = "media.discard"
    DOCUMENT_PERSIST = "document.persist"
    DOCUMENT_FORMATS = "document.formats"
    # chat.send 上行外部 url 文件导入（Phase 2：AgentServer 下载落盘注入目录，Gateway 不落盘）
    FILE_IMPORT_URL = "file.import_url"
    # 分块上传：用于 AgentOS 多用户场景的大文件，避免单个 E2A WebSocket 帧超过限制。
    FILE_UPLOAD_CHUNK = "file.upload_chunk"
    # Smart Approval sealed assets: validate and read one bounded chunk in the
    # routed AgentServer. Gateway must never authorize these from token paths.
    FILE_DOWNLOAD_VERIFIED_CHUNK = "file.download_verified_chunk"

    # IM 平台附件落盘（Phase 3：Gateway 下载字节后经 base64 交给 AgentServer
    # 落盘至其注入目录的 <平台>_files/downloads/，Gateway 不直写用户目录）
    IM_FILE_PERSIST = "im.file_persist"

    # Gateway cron 单源的只读内存快照。AgentServer 只消费该快照供本轮
    # cron 工具查询/更新，不持久化、不恢复、更不会启动本地调度器。
    CRON_JOBS_SYNC = "cron.jobs.sync"
    CRON_COMMAND_ACK = "cron.command.ack"
    CRON_RUN_NOW_ACK = "cron.run_now.ack"

    # Gateway owns voice task records; AgentServer returns execution facts/files.
    VOICE_TASK_CHECKPOINT_ACK = "voice.task.checkpoint.ack"
    VOICE_TASK_FILES = "voice.task.files"
    # Read-only: whether Core still waits for an answer in the session (#5486).
    VOICE_TASK_PENDING_INTERRUPT = "voice.task.pending_interrupt"
    VOICE_SESSION_RECEIPTS = "voice.session.receipts"

    # HarmonyOS TUI DevEco bootstrap（Phase 3：用户态在目标 AgentServer 注入目录执行）
    HARMONYOS_PROJECT_INIT = "harmonyos.project_init"
    HARMONYOS_DEV_INIT = "harmonyos.dev_init"

    TTS_SYNTHESIZE = "tts.synthesize"

    AGENTS_LIST = "agents.list"
    AGENTS_GET = "agents.get"
    AGENTS_CREATE = "agents.create"
    AGENTS_UPDATE = "agents.update"
    AGENTS_DELETE = "agents.delete"
    AGENTS_ENABLE = "agents.enable"
    AGENTS_DISABLE = "agents.disable"
    AGENTS_TOOLS_LIST = "agents.tools_list"
    AGENT_SWITCH = "3rdagent.switch"
    AGENT_LIST = "3rdagent.list"

    # Unified asset publishing; dispatched by AgentServer independently of chats.
    ASSETS_PUBLISH_DESCRIBE = "assets.publish.describe"
    ASSETS_PUBLISH_PREPARE = "assets.publish.prepare"
    ASSETS_PUBLISH_COMMIT = "assets.publish.commit"
    ASSETS_PUBLISH_STATUS = "assets.publish.status"
    ASSETS_PUBLISH_RECORDS = "assets.publish.records"
    ASSETS_PUBLISH_LOCAL_STATUS = "assets.publish.local_status"

    # mcp management.
    MCP_LIST = "mcp.list"
    MCP_SHOW = "mcp.show"
    MCP_INSTALL = "mcp.install"
    MCP_UNINSTALL = "mcp.uninstall"
    MCP_CONNECT = "mcp.connect"
    MCP_WAIT_AUTH = "mcp.wait_auth"
    MCP_CANCEL_CONNECT = "mcp.cancel_connect"
    MCP_DISCONNECT = "mcp.disconnect"
    MCP_REGISTER_CUSTOM = "mcp.register_custom"
    MCP_DELETE_CUSTOM = "mcp.delete_custom"
    MCP_SAVE_CREDENTIALS = "mcp.save_credentials"

    SKILLS_MARKETPLACE_LIST = "skills.marketplace.list"
    SKILLS_LIST = "skills.list"
    SKILLS_INSTALLED = "skills.installed"
    SKILLS_GET = "skills.get"
    SKILLS_VERSIONS_LIST = "skills.versions.list"
    SKILLS_FILES_LIST = "skills.files.list"
    SKILLS_FILES_GET = "skills.files.get"
    SKILLS_REBUILD = "skills.rebuild"
    SKILLS_TOGGLE = "skills.toggle"
    # Per-workspace Skill visibility (team mode): the Skill entities themselves
    # live in exactly one global library, so who may see which Skill is metadata
    # stored next to a member / team workspace rather than a directory layout.
    SKILLS_VISIBILITY_GET = "skills.visibility.get"
    SKILLS_VISIBILITY_SET = "skills.visibility.set"
    SKILLS_VISIBILITY_UPDATE = "skills.visibility.update"
    SKILLS_INSTALL = "skills.install"
    SKILLS_PACK_MEMBER_INSTALL = "skills.pack_member.install"
    SKILLS_IMPORT_LOCAL = "skills.import_local"
    SKILLS_IMPORT_UPLOAD = "skills.import_upload"
    SKILLS_CREATE_FROM_KNOWLEDGE = "skills.create_from_knowledge"
    SKILLS_MARKETPLACE_ADD = "skills.marketplace.add"
    SKILLS_MARKETPLACE_REMOVE = "skills.marketplace.remove"
    SKILLS_MARKETPLACE_TOGGLE = "skills.marketplace.toggle"
    SKILLS_UNINSTALL = "skills.uninstall"
    SKILLS_ONLINE_SEARCH = "skills.online_search.search"
    SKILLS_ONLINE_SEARCH_INSTALL = "skills.online_search.install"
    SKILLS_SKILLNET_SEARCH = "skills.skillnet.search"
    SKILLS_SKILLNET_INSTALL = "skills.skillnet.install"
    SKILLS_SKILLNET_INSTALL_STATUS = "skills.skillnet.install_status"
    SKILLS_SKILLNET_EVALUATE = "skills.skillnet.evaluate"
    SKILLS_CLAWHUB_GET_TOKEN = "skills.clawhub.get_token"
    SKILLS_CLAWHUB_SET_TOKEN = "skills.clawhub.set_token"
    SKILLS_CLAWHUB_SEARCH = "skills.clawhub.search"
    SKILLS_CLAWHUB_DOWNLOAD = "skills.clawhub.download"
    SKILLS_TEAMSKILLS_HUB_INFO = "skills.teamskillshub.info"
    SKILLS_TEAMSKILLS_HUB_INIT = "skills.teamskillshub.init"
    SKILLS_TEAMSKILLS_HUB_VALIDATE = "skills.teamskillshub.validate"
    SKILLS_TEAMSKILLS_HUB_PACK = "skills.teamskillshub.pack"
    SKILLS_TEAMSKILLS_HUB_SEARCH = "skills.teamskillshub.search"
    SKILLS_SWARMSKILLS_HUB_RECOMMEND = "skills.swarmskillshub.recommend"
    SKILLS_TEAMSKILLS_HUB_INSTALL = "skills.teamskillshub.install"
    SKILLS_TEAMSKILLS_HUB_PUBLISH = "skills.teamskillshub.publish"
    SKILLS_TEAMSKILLS_HUB_DELETE = "skills.teamskillshub.delete"
    SKILLS_SWARMSKILLS_HUB_DETAIL = "skills.swarmskillshub.detail"
    SKILLS_RETRIEVAL_STATUS = "skills.retrieval.status"
    SKILLS_RETRIEVAL_INDEX_BUILD = "skills.retrieval.index_build"
    SKILLS_RETRIEVAL_INDEX_CANCEL = "skills.retrieval.index_cancel"
    SKILLS_RETRIEVAL_SEARCH = "skills.retrieval.search"
    SKILLS_RETRIEVAL_TREE = "skills.retrieval.tree"
    SKILLS_EVOLUTION_STATUS = "skills.evolution.status"
    SKILLS_EVOLUTION_GET = "skills.evolution.get"
    SKILLS_EVOLUTION_SAVE = "skills.evolution.save"

    # Skill Graph Web panel transport. The implementation is provided by
    # agent-core Symphony, while the public transport remains skill-domain API.
    SKILLS_GRAPH_BUILD = "skills.graph.build"
    SKILLS_GRAPH_STATUS = "skills.graph.status"
    SKILLS_GRAPH_GET = "skills.graph.get"
    SKILLS_GRAPH_CANCEL = "skills.graph.cancel"
    SKILLS_EXPERIENCE_LIST = "skills.experience.list"
    SKILLS_EXPERIENCE_REQUEST = "skills.experience.request"

    # Task (session) assets: named files referenceable with @name in the chat box.
    SESSION_ASSETS_LIST = "session.assets.list"
    SESSION_ASSETS_REGISTER = "session.assets.register"
    SESSION_ASSETS_RENAME = "session.assets.rename"
    # Removing a composer attachment chip should also drop the session-asset
    # registry entry it created (see session_assets.py). staging/develop add
    # this via _ensure_req_method's protocol-gap shim; here it's a native member.
    SESSION_ASSETS_REMOVE = "session.assets.remove"

    # 技能同步（Server / Client 分离部署）。外部入口为 HTTP /skill-sync/*
    # （Bearer token 鉴权）。WS 侧仅保留 reload：web 进程 HTTP apply 落盘后的
    # 进程间通知（参数仅 applied 名称列表，纯 JSON）；diff / package / apply
    # 不注册 WS 路由（经 WS 调用会绕过 token 鉴权，且 package 泄漏临时目录、
    # apply 的 zip_bytes 无法过 JSON 序列化，均不可用）。
    SKILL_SYNC_DIFF = "skills.sync.diff"
    SKILL_SYNC_PACKAGE = "skills.sync.package"
    SKILL_SYNC_APPLY = "skills.sync.apply"
    SKILL_SYNC_RELOAD = "skills.sync.reload"

    PERSONAL_CONTEXT_RUNTIME_STATUS = "personal_context.runtime.status"
    PERSONAL_CONTEXT_RUNTIME_START_COLLECTION = (
        "personal_context.runtime.start_collection"
    )
    PERSONAL_CONTEXT_RUNTIME_STOP_COLLECTION = (
        "personal_context.runtime.stop_collection"
    )
    PERSONAL_CONTEXT_RUNTIME_START_AGENT_USE = (
        "personal_context.runtime.start_agent_use"
    )
    PERSONAL_CONTEXT_RUNTIME_STOP_AGENT_USE = "personal_context.runtime.stop_agent_use"
    PERSONAL_CONTEXT_RUNTIME_SET_MASTER_ENABLED = (
        "personal_context.runtime.set_master_enabled"
    )
    PERSONAL_CONTEXT_RUNTIME_GET_CONFIG = "personal_context.runtime.get_config"
    PERSONAL_CONTEXT_RUNTIME_PATCH_CONFIG = "personal_context.runtime.patch_config"
    PERSONAL_CONTEXT_RUNTIME_SELECT_MODEL = "personal_context.runtime.select_model"
    PERSONAL_CONTEXT_FETCH_LIST_SERVICES = "personal_context.fetch.list_services"
    PERSONAL_CONTEXT_FETCH_CREATE_SERVICE = "personal_context.fetch.create_service"
    PERSONAL_CONTEXT_FETCH_DELETE_SERVICE = "personal_context.fetch.delete_service"
    PERSONAL_CONTEXT_FETCH_PATCH_SERVICE = "personal_context.fetch.patch_service"
    PERSONAL_CONTEXT_FETCH_START_SERVICE = "personal_context.fetch.start_service"
    PERSONAL_CONTEXT_FETCH_STOP_SERVICE = "personal_context.fetch.stop_service"
    PERSONAL_CONTEXT_FETCH_RUN_ALL = "personal_context.fetch.run_all"
    PERSONAL_CONTEXT_FETCH_RUN_ONE = "personal_context.fetch.run_one"
    PERSONAL_CONTEXT_FETCH_STOP_RUN = "personal_context.fetch.stop_run"
    PERSONAL_CONTEXT_FETCH_GET_RUN_STATUS = "personal_context.fetch.get_run_status"
    PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS = (
        "personal_context.fetch.get_authorization_status"
    )
    PERSONAL_CONTEXT_FETCH_AUTHORIZE_PROVIDER = (
        "personal_context.fetch.authorize_provider"
    )
    PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH = "personal_context.context.stream_graph"
    PERSONAL_CONTEXT_CONTEXT_STREAM_TREE = "personal_context.context.stream_tree"
    PERSONAL_CONTEXT_CONTEXT_SEARCH_PAGES = "personal_context.context.search_pages"
    PERSONAL_CONTEXT_CONTEXT_GET_NODE = "personal_context.context.get_node"
    PERSONAL_CONTEXT_CONTEXT_GET_SOURCE = "personal_context.context.get_source"

    # Plugin management (reuses skills marketplace infrastructure)
    PLUGINS_LIST = "plugins.list"
    PLUGINS_INSTALL = "plugins.install"
    PLUGINS_UNINSTALL = "plugins.uninstall"
    PLUGINS_ENABLE = "plugins.enable"
    PLUGINS_DISABLE = "plugins.disable"
    PLUGINS_RELOAD = "plugins.reload"

    EXTENSIONS_LIST = "extensions.list"
    EXTENSIONS_IMPORT = "extensions.import"
    EXTENSIONS_DELETE = "extensions.delete"
    EXTENSIONS_TOGGLE = "extensions.toggle"

    # AgentGroup selection + agent_template / plugin package catalog RPCs.
    AGENT_GROUPS_LIST = "agent_groups.list"
    AGENT_GROUPS_SHOW = "agent_groups.show"
    AGENT_GROUPS_FILE_LIST = "agent_groups.file.list"
    AGENT_GROUPS_FILE_READ = "agent_groups.file.read"
    AGENT_GROUPS_CREATE = "agent_groups.create"
    AGENT_GROUPS_IMPORT_LOCAL = "agent_groups.import_local"
    AGENT_GROUPS_INSTALL = "agent_groups.install"
    AGENT_GROUPS_UNINSTALL = "agent_groups.uninstall"
    AGENT_TEMPLATES_LIST = "agent_templates.list"
    AGENT_TEMPLATES_SHOW = "agent_templates.show"
    AGENT_TEMPLATES_FILE_LIST = "agent_templates.file.list"
    AGENT_TEMPLATES_FILE_READ = "agent_templates.file.read"
    AGENT_TEMPLATES_CREATE = "agent_templates.create"
    AGENT_TEMPLATES_UPDATE = "agent_templates.update"
    AGENT_TEMPLATES_DELETE = "agent_templates.delete"
    AGENT_TEMPLATES_IMPORT_LOCAL = "agent_templates.import_local"
    AGENT_TEMPLATES_INSTALL = "agent_templates.install"
    AGENT_TEMPLATES_UNINSTALL = "agent_templates.uninstall"
    PLUGIN_PACKAGES_LIST = "plugin_packages.list"
    PLUGIN_PACKAGES_SHOW = "plugin_packages.show"
    PLUGIN_PACKAGES_CREATE = "plugin_packages.create"
    PLUGIN_PACKAGES_IMPORT_LOCAL = "plugin_packages.import_local"
    PLUGIN_PACKAGES_INSTALL = "plugin_packages.install"
    PLUGIN_PACKAGES_UNINSTALL = "plugin_packages.uninstall"

    HOOKS_LIST = "hooks.list"

    # 旧探活使用 health_check 命名空间。
    HEALTH_CHECK_GET_CONF = "health_check.get_conf"
    HEALTH_CHECK_SET_CONF = "health_check.set_conf"
    # Deprecated probe RPC aliases kept for clients upgrading across the rename.
    HEARTBEAT_GET_CONF = "heartbeat.get_conf"
    HEARTBEAT_SET_CONF = "heartbeat.set_conf"

    # Gateway -> AgentServer proxy for AgentServer-owned Heartbeat job operations.
    HEARTBEAT_JOB = "heartbeat.job"

    # 安全防护 permissions（与 Web ``register_method`` 同名，经 E2A → AgentServer 处理；owner_scopes 仅走 Web 直连）
    PERMISSIONS_TOOLS_GET = "permissions.tools.get"
    PERMISSIONS_TOOLS_SET = "permissions.tools.set"
    PERMISSIONS_TOOLS_UPDATE = "permissions.tools.update"
    PERMISSIONS_TOOLS_UPDATE_USER = "permissions.tools.update_user"
    PERMISSIONS_TOOLS_DELETE = "permissions.tools.delete"
    PERMISSIONS_RULES_GET = "permissions.rules.get"
    PERMISSIONS_RULES_CREATE = "permissions.rules.create"
    PERMISSIONS_RULES_UPDATE = "permissions.rules.update"
    PERMISSIONS_RULES_DELETE = "permissions.rules.delete"
    PERMISSIONS_APPROVAL_OVERRIDES_GET = "permissions.approval_overrides.get"
    PERMISSIONS_APPROVAL_OVERRIDES_DELETE = "permissions.approval_overrides.delete"
    PERMISSIONS_OWNER_SCOPES_GET = "permissions.owner_scopes.get"
    PERMISSIONS_OWNER_SCOPES_SET = "permissions.owner_scopes.set"

    MEMORY_FORBIDDEN_GET = "memory.forbidden.get"
    MEMORY_FORBIDDEN_SET = "memory.forbidden.set"

    # A4P 授权桥接（Web User Authorizer → AgentServer A4PServer）
    A4P_AUTHORIZATION_REPREPARE = "a4p.authorization.reprepare"
    A4P_AUTHORIZATION_COMPLETE = "a4p.authorization.complete"
    A4P_AUTHORIZATION_REJECT = "a4p.authorization.reject"
    A4P_AUTHORIZATION_PENDING = "a4p.authorization.pending"
    A4P_CONFIG_GET = "a4p.config.get"
    A4P_CONFIG_UPDATE = "a4p.config.update"

    CHANNEL_FEISHU_GET_CONF = "channel.feishu.get_conf"
    CHANNEL_FEISHU_SET_CONF = "channel.feishu.set_conf"

    CHANNEL_XIAOYI_GET_CONF = "channel.xiaoyi.get_conf"
    CHANNEL_XIAOYI_SET_CONF = "channel.xiaoyi.set_conf"

    CHANNEL_TELEGRAM_GET_CONF = "channel.telegram.get_conf"
    CHANNEL_TELEGRAM_SET_CONF = "channel.telegram.set_conf"
    CHANNEL_SLACK_GET_CONF = "channel.slack.get_conf"
    CHANNEL_SLACK_SET_CONF = "channel.slack.set_conf"
    CHANNEL_DINGTALK_GET_CONF = "channel.dingtalk.get_conf"
    CHANNEL_DINGTALK_SET_CONF = "channel.dingtalk.set_conf"

    CHANNEL_WHATSAPP_GET_CONF = "channel.whatsapp.get_conf"
    CHANNEL_WHATSAPP_SET_CONF = "channel.whatsapp.set_conf"
    CHANNEL_WECHAT_GET_CONF = "channel.wechat.get_conf"
    CHANNEL_WECHAT_SET_CONF = "channel.wechat.set_conf"
    CHANNEL_WECHAT_GET_LOGIN_UI = "channel.wechat.get_login_ui"
    CHANNEL_WECHAT_UNBIND = "channel.wechat.unbind"

    UPDATER_GET_STATUS = "updater.get_status"
    UPDATER_CHECK = "updater.check"
    UPDATER_DOWNLOAD = "updater.download"
    UPDATER_GET_CONF = "updater.get_conf"
    UPDATER_SET_CONF = "updater.set_conf"

    TEAM_SNAPSHOT = "team.snapshot"
    TEAM_HISTORY_GET = "team.history.get"
    TEAM_MEMBERS_GET = "team.members.get"
    TEAM_MQ_PUBLISH = "team.mq.publish"
    TEAM_LIST = "team.list"
    ORG_SNAPSHOT = "org.snapshot"

    # Harness package management
    HARNESS_PACKAGES_GET = "harness.packages.get"
    HARNESS_PACKAGES_SCAN = "harness.packages.scan"
    HARNESS_PACKAGES_ACTIVATE = "harness.packages.activate"
    HARNESS_PACKAGES_DEACTIVATE = "harness.packages.deactivate"
    HARNESS_PACKAGES_DELETE = "harness.packages.delete"
    HARNESS_PACKAGES_IMPORT = "harness.packages.import"
    HARNESS_PACKAGES_EXPORT = "harness.packages.export"

    # RSI 优化平台（web → gateway → agentserver 全链路，web 契约 v0.3 §4）
    RSI_DATASET_VALIDATE = "rsi.dataset.validate"
    RSI_TASK_CREATE = "rsi.task.create"
    RSI_TASK_LIST = "rsi.task.list"
    RSI_TASK_GET = "rsi.task.get"
    RSI_TASK_DELETE = "rsi.task.delete"
    RSI_TRAINING_START = "rsi.training.start"
    RSI_TRAINING_PAUSE = "rsi.training.pause"
    RSI_TRAINING_RESUME = "rsi.training.resume"
    RSI_TRAINING_TERMINATE = "rsi.training.terminate"
    RSI_REPORT_GET = "rsi.report.get"
    RSI_USAGE_GET = "rsi.usage.get"
    RSI_ARTIFACT_DOWNLOAD = "rsi.artifact.download"
    RSI_ARTIFACT_FILES_LIST = "rsi.artifact.files.list"
    RSI_ARTIFACT_FILES_GET = "rsi.artifact.files.get"
    RSI_TREE_GET = "rsi.tree.get"
    RSI_HARNESS_INSTALL = "rsi.harness.install"
    RSI_HARNESS_VERSIONS_LIST = "rsi.harness.versions.list"
    RSI_HARNESS_ROLLBACK = "rsi.harness.rollback"

    # Trajectory diagnosis: the Gateway proxies these to the Worker-hosted service
    DIAGNOSIS_RUN = "diagnosis.run"
    DIAGNOSIS_LIST = "diagnosis.list"
    DIAGNOSIS_GET = "diagnosis.get"
    DIAGNOSIS_CANCEL = "diagnosis.cancel"
    DIAGNOSIS_DELETE = "diagnosis.delete"
    DIAGNOSIS_TTSE_TRIGGER = "diagnosis.ttse.trigger"

    # Schedule task management
    SCHEDULE_CHECK_CONFIG = "schedule.check_config"
    SCHEDULE_UPDATE_CONFIG = "schedule.update_config"
    SCHEDULE_CREATE = "schedule.create"
    SCHEDULE_RUN = "schedule.run"
    SCHEDULE_LIST = "schedule.list"
    SCHEDULE_STATUS = "schedule.status"
    SCHEDULE_LOGS = "schedule.logs"
    SCHEDULE_CANCEL = "schedule.cancel"
    SCHEDULE_DELETE = "schedule.delete"
    ISSUE_WATCH_ONCE = "issue.watch_once"
    ISSUE_STATE_LIST = "issue.state.list"
    ISSUE_DELETE = "issue.delete"
    ISSUE_MATRIX = "issue.matrix"


class EventType(Enum):
    SESSION_ARCHIVED = "session.archived"
    SESSION_UNARCHIVED = "session.unarchived"
    SESSION_DELETED = "session.deleted"
    SESSION_LIFECYCLE_UPDATED = "session.lifecycle.updated"
    PROJECT_LIFECYCLE_UPDATED = "project.lifecycle.updated"
    # 项目移除(软删除)/恢复：其会话与定时任务的可见性随之变化，
    # 其他端必须据此刷新工作区、归档页与定时任务列表。
    PROJECT_REMOVED = "project.removed"
    PROJECT_RESTORED = "project.restored"
    CONNECTION_ACK = "connection.ack"
    HELLO = "hello"
    CHAT_DELTA = "chat.delta"
    CHAT_REASONING = "chat.reasoning"
    CHAT_USAGE_METADATA = "chat.usage_metadata"
    CHAT_USAGE_SUMMARY = "chat.usage_summary"
    CHAT_FINAL = "chat.final"
    CHAT_RETRACT = "chat.retract"
    CHAT_MEDIA = "chat.media"
    CHAT_FILE = "chat.file"
    CHAT_TOOL_CALL = "chat.tool_call"
    CHAT_TOOL_UPDATE = "chat.tool_update"
    CHAT_TOOL_RESULT = "chat.tool_result"
    CHAT_SYMPHONY_STATUS = "chat.symphony_status"
    CHAT_MESSAGE_UPDATED = "chat.message_updated"
    CONTEXT_USAGE = "context.usage"
    TODO_UPDATED = "todo.updated"
    CHAT_PROCESSING_STATUS = "chat.processing_status"
    CHAT_ERROR = "chat.error"
    CHAT_INTERRUPT_RESULT = "chat.interrupt_result"
    CHAT_EVOLUTION_STATUS = "chat.evolution_status"
    CHAT_SUBTASK_UPDATE = "chat.subtask_update"
    CHAT_SUBAGENT_ACTIVITY = "chat.subagent_activity"
    CHAT_ASK_USER_QUESTION = "chat.ask_user_question"
    PLAN_APPROVAL_REQUIRED = "plan.approval_required"
    CHAT_SESSION_RESULT = "chat.session_result"
    GOAL_SNAPSHOT = "goal.snapshot"
    GOAL_UPDATED = "goal.updated"
    RUNTIME_ACCEPTED = "runtime.accepted"
    EXECUTION_ERROR = "execution.error"
    TEAM_MEMBER = "team.member"
    TEAM_TASK = "team.task"
    TEAM_MESSAGE = "team.message"
    WORKFLOW_UPDATED = "workflow.updated"
    DESIGNER_RUN_UPDATED = "designer.run.updated"
    DESIGNER_NODE_UPDATED = "designer.node.updated"
    DESIGNER_GRAPH_UPDATED = "designer.graph.updated"
    DESIGNER_LEADER_ACTIVITY = "designer.leader.activity"
    # 旧探活结果通过 health_check.relay 发送。
    # 新心跳任务(heartbeat.job.*)不使用 relay 事件,结果通过普通 chat.send 进入原会话。
    HEALTH_CHECK_RELAY = "health_check.relay"
    # Deprecated source-level alias. Legacy wire frames are normalized by
    # _missing_ so every downstream channel sees HEALTH_CHECK_RELAY.
    HEARTBEAT_RELAY = "health_check.relay"
    HISTORY_GET = "history.message"
    PROACTIVE_RECOMMENDATION = "proactive_recommendation"

    @classmethod
    def _missing_(cls, value):
        if value == "heartbeat.relay":
            return cls.HEALTH_CHECK_RELAY
        return None


class Mode(Enum):
    AGENT = "agent"
    # 历史值：plan / fast 已合并为 agent，保留以兼容旧序列化数据的反解析。
    AGENT_PLAN = "agent.plan"
    AGENT_FAST = "agent.fast"
    CODE_PLAN = "code.plan"
    CODE_NORMAL = "code.normal"
    CODE_TEAM = "code.team"
    TEAM = "team"
    TEAM_PLAN_NORMAL = "team.plan.normal"
    TEAM_PLAN_CODE = "team.plan.code"
    # 新三段命名 canonical（P2 引入；旧成员保留以兼容历史持久化反解析）。
    AGENT_WORK_NORMAL = "agent.work.normal"
    AGENT_WORK_PLAN = "agent.work.plan"
    AGENT_CODE_NORMAL = "agent.code.normal"
    AGENT_CODE_PLAN = "agent.code.plan"
    TEAM_WORK_NORMAL = "team.work.normal"
    TEAM_WORK_PLAN = "team.work.plan"
    TEAM_CODE_NORMAL = "team.code.normal"
    TEAM_CODE_PLAN = "team.code.plan"

    @classmethod
    def from_raw(cls, raw_mode: Any, default: "Mode | None" = None) -> "Mode":
        """解析 mode：新 canonical 原样返回，旧 canonical 静默映射到新 canonical。

        单一逻辑路径——不再维护手写白名单：``deprecate_mode`` 对新 canonical
        原样返回（不在 :data:`DEPRECATION_MAP` 里），对旧 canonical 映射到新串，
        对未知串原样返回后由 ``cls(...)`` 抛 ``ValueError`` 兜底到 ``fallback``。
        裸 ``plan`` / ``fast`` 与 CLI ``MODE_ALIASES`` 同语义，直接落到
        :attr:`AGENT_WORK_NORMAL`，不绕 ``agent.fast`` 中间步。
        """
        fallback = default or cls.AGENT_WORK_NORMAL
        if isinstance(raw_mode, Mode):
            raw_mode = raw_mode.value
        if not isinstance(raw_mode, str):
            return fallback
        normalized = raw_mode.strip().lower()
        if not normalized:
            return fallback
        if normalized in ("plan", "fast"):
            logger.debug("Mode.from_raw: bare '%s' -> AGENT_WORK_NORMAL", normalized)
            return cls.AGENT_WORK_NORMAL
        new_mode_str = deprecate_mode(normalized)
        try:
            mode = cls(new_mode_str)
        except ValueError:
            logger.warning(
                "Mode.from_raw: 无法识别的 mode=%r (deprecate 后=%r)，兜底 %s",
                raw_mode, new_mode_str, fallback.value,
||||||| e487e9a30
    CHAT_SEND = "chat.send"
    CHAT_RESUME = "chat.resume"
    CHAT_CANCEL = "chat.interrupt"
    CHAT_ANSWER = "chat.user_answer"
    CHAT_SWARMFLOW_REPLY = "chat.swarmflow_reply"
    SSH_RELAY = "ssh.relay"
    HISTORY_GET = "history.get"
    GROUP_CREATE = "group.create"
    GROUP_DELETE = "group.delete"
    GROUP_LIST = "group.list"
    GROUP_GET = "group.get"
    GROUP_UPDATE = "group.update"
    GROUP_MEMBER_ADD = "group.member.add"
    GROUP_HISTORY_GET = "group.history.get"
    COMMAND_BTW = "command.btw"
    COMMAND_ADD_DIR = "command.add_dir"
    COMMAND_CHROME = "command.chrome"
    COMMAND_COMPACT = "command.compact"
    COMMAND_COMPACT_PARTIAL = "command.compact_partial"
    COMMAND_CONTEXT = "command.context"
    COMMAND_RECAP = "command.recap"
    COMMAND_DIFF = "command.diff"
    COMMAND_SIMPLIFY = "command.simplify"
    COMMAND_MCP = "command.mcp"
    COMMAND_MODEL = "command.model"
    COMMAND_RESUME = "command.resume"
    COMMAND_SANDBOX = "command.sandbox"
    COMMAND_SESSION = "command.session"
    COMMAND_WORKFLOWS = "command.workflows"
    COMMAND_STATUS = "command.status"
    SWARMFLOW_PAUSE = "swarmflow.pause"
    SWARMFLOW_RESUME = "swarmflow.resume"
    SWARMFLOW_STOP = "swarmflow.stop"

    CONFIG_GET = "config.get"
    CONFIG_MOA_STATUS = "config.moa_status"
    CONFIG_SET = "config.set"
    CONFIG_SAVE_ALL = "config.save_all"
    CONFIG_VALIDATE_MODEL = "config.validate_model"
    MODELS_LIST = "models.list"
    MODELS_ZEN_ENTRIES = "models.zen_entries"
    MODELS_ZEN_WARM = "models.zen_warm"
    MODELS_GET = "models.get"
    MODELS_REPLACE_ALL = "models.replace_all"
    MODELS_VALIDATE = "models.validate"
    MODELS_UPSERT = "models.upsert"
    MODELS_DELETE = "models.delete"
    MODELS_REFERENCES = "models.references"
    MODEL_GROUPS_LIST = "model_groups.list"
    MODEL_GROUPS_UPSERT = "model_groups.upsert"
    MODEL_GROUPS_DELETE = "model_groups.delete"
    SESSION_SELECTION_SET = "session.selection.set"
    LOCALE_GET_CONF = "locale.get_conf"
    LOCALE_SET_CONF = "locale.set_conf"
    CHANNEL_GET = "channel.get"

    SESSION_LIST = "session.list"
    SESSION_GET_METADATA = "session.get_metadata"
    SESSION_MESSAGE_CONTINUE_QUEUED = "session.message.continue_queued"
    SESSION_PLAN_STATUS = "session.plan_status"
    SESSION_PIN = "session.pin"
    SESSION_COLOR_SET = "session.color_set"
    SESSION_PREVIEW = "session.preview"
    SESSION_CREATE = "session.create"
    SESSION_SWITCH = "session.switch"
    SESSION_DELETE = "session.delete"
    SESSION_INPUT_INTENT = "session.input.intent"
    SESSION_RENAME = "session.rename"
    SESSION_FORK = "session.fork"
    SESSION_REBIND_PROJECT = "session.rebind_project"
    SESSION_REWIND = "session.rewind"
    SESSION_REWIND_AND_RESTORE = "session.rewind_and_restore"
    SESSION_REWIND_CONTEXT = "session.rewind_context"
    SESSION_REWIND_COMPACT = "session.rewind_compact"
    SESSION_RESTORE_FILES = "session.restore_files"
    HISTORY_LIST_TURNS = "history.list_turns"
    HISTORY_APPEND_RECORD = "history.append_record"
    TEAM_TEMPLATES_LIST = "team.templates.list"
    TEAM_BINDINGS_LIST = "team.bindings.list"
    TEAM_BINDING_CREATE = "team.binding.create"
    TEAM_BINDING_GENERATE = "team.binding.generate"
    TEAM_SESSION_BIND = "team.session.bind"
    TEAM_DELETE = "team.delete"

    PATH_GET = "path.get"
    PATH_SET = "path.set"
    PATH_SELECT_DIRECTORY = "path.select_directory"
    PATH_SELECT_FILES = "path.select_files"

    BROWSER_RUNTIME_RESTART = "browser.runtime_restart"

    CONFIG_CACHE_CLEAR = "config.cache_clear"
    AGENT_RELOAD_CONFIG = "agent.reload_config"
    AGENT_PREWARM_SYNC = "agent.prewarm.sync"
    # Gateway → AgentServer：登录模型凭据续期后的新 token / 会话注销后的撤销
    AUTH_CREDENTIALS_UPDATE = "auth.credentials.update"

    MEMORY_COMPUTE = "memory.compute"
    # TUI memory management (Phase 3: execute in the target AgentServer's
    # injected user directory; Gateway only forwards the request).
    MEMORY_LIST = "memory.list"
    MEMORY_EDIT = "memory.edit"
    MEMORY_STATUS = "memory.status"
    MEMORY_TOGGLE = "memory.toggle"
    MEMORY_OPEN = "memory.open"

    # Project domain (Phase 3).  The Gateway routes these calls but all
    # project-store and session-metadata access happens in AgentServer.
    PROJECT_INFO = "project.info"
    PROJECT_PINNED_SESSIONS = "project.pinned_sessions"
    PROJECT_GET_SESSIONS = "project.get_sessions"
    PROJECT_GET_CRON_SESSIONS = "project.get_cron_sessions"
    # Resolve a cron project's binding in the selected AgentServer directory.
    # Gateway keeps the job store/scheduler but must not inspect user projects.
    PROJECT_CRON_RESOLVE_BINDING = "project.cron.resolve_binding"
    PROJECT_LIST = "project.list"
    PROJECT_CREATE = "project.create"
    PROJECT_RENAME = "project.rename"
    PROJECT_PIN = "project.pin"
    SESSION_ARCHIVE = "session.archive"
    SESSION_UNARCHIVE = "session.unarchive"
    SESSION_ARCHIVED_LIST = "session.archived.list"
    CRON_SESSIONS_DELETE = "cron.sessions.delete"
    PROJECT_REMOVE = "project.remove"
    PROJECT_RESTORE = "project.restore"
    PROJECT_LIFECYCLE = "project.lifecycle"
    PROJECT_SESSIONS_ARCHIVE = "project.sessions.archive"
    PROJECT_SESSIONS_DELETE_ARCHIVED = "project.sessions.delete_archived"
    PROJECT_GIT_STATUS = "project.git.status"
    PROJECT_GIT_PROBE = "project.git.probe"
    PROJECT_GIT_INIT = "project.git.init"
    PROJECT_GIT_SWITCH_BRANCH = "project.git.switch_branch"
    PROJECT_GIT_CREATE_BRANCH = "project.git.create_branch"
    PROJECT_GIT_COMMIT = "project.git.commit"
    PROJECT_GIT_PUSH = "project.git.push"
    PROJECT_GIT_DIFF_STATUS = "project.git.diff_status"
    PROJECT_GIT_TURN_DIFF_LIST = "project.git.turn_diff_list"
    PROJECT_GIT_TURN_DIFF = "project.git.turn_diff"
    PROJECT_GIT_DISCARD_TURN_CHANGES = "project.git.discard_turn_changes"
    PROJECT_GIT_REDO_TURN_CHANGES = "project.git.redo_turn_changes"

    # Designer execution graph (canvas + subagent orchestration shell)
    DESIGNER_WORKSPACE_CREATE = "designer.workspace.create"
    DESIGNER_WORKSPACE_GET = "designer.workspace.get"
    DESIGNER_WORKSPACE_SESSION_CREATE = "designer.workspace.session.create"
    DESIGNER_WORKSPACE_SESSION_COMPOSE = "designer.workspace.session.compose"
    DESIGNER_GRAPH_GET = "designer.graph.get"
    DESIGNER_GRAPH_LIST = "designer.graph.list"
    DESIGNER_GRAPH_SAVE = "designer.graph.save"
    DESIGNER_GRAPH_BOOTSTRAP = "designer.graph.bootstrap"
    DESIGNER_GRAPH_PATCH = "designer.graph.patch"
    DESIGNER_GRAPH_CHAT = "designer.graph.chat"
    DESIGNER_MEDIA_CAPABILITIES = "designer.media.capabilities"
    DESIGNER_RUN_START = "designer.run.start"
    DESIGNER_RUN_GET = "designer.run.get"
    DESIGNER_RUN_PAUSE = "designer.run.pause"
    DESIGNER_RUN_CANCEL = "designer.run.cancel"
    DESIGNER_RUN_CHOOSE_OUTPUT = "designer.run.choose_output"

    PROACTIVE_TICK = "proactive.tick"  # Trigger proactive recommendation tick (from Cron)
    PROACTIVE_FEEDBACK = "proactive.feedback"  # User feedback on proactive recommendation (like/dislike)
    COMMAND_GOAL = "command.goal"
    COMMANDS_LIST = "commands.list"

    FILES_LIST = "files.list"
    FILES_GET = "files.get"

    # 媒体/文档附件（Phase 2 WorkspaceFileAdapter）
    MEDIA_PERSIST = "media.persist"
    # 丢弃尚未发送的会话 uploads 副本。只删当前会话 uploads 内的普通文件。
    MEDIA_DISCARD = "media.discard"
    DOCUMENT_PERSIST = "document.persist"
    DOCUMENT_FORMATS = "document.formats"
    # chat.send 上行外部 url 文件导入（Phase 2：AgentServer 下载落盘注入目录，Gateway 不落盘）
    FILE_IMPORT_URL = "file.import_url"
    # 分块上传：用于 AgentOS 多用户场景的大文件，避免单个 E2A WebSocket 帧超过限制。
    FILE_UPLOAD_CHUNK = "file.upload_chunk"
    # Smart Approval sealed assets: validate and read one bounded chunk in the
    # routed AgentServer. Gateway must never authorize these from token paths.
    FILE_DOWNLOAD_VERIFIED_CHUNK = "file.download_verified_chunk"

    # IM 平台附件落盘（Phase 3：Gateway 下载字节后经 base64 交给 AgentServer
    # 落盘至其注入目录的 <平台>_files/downloads/，Gateway 不直写用户目录）
    IM_FILE_PERSIST = "im.file_persist"

    # Gateway cron 单源的只读内存快照。AgentServer 只消费该快照供本轮
    # cron 工具查询/更新，不持久化、不恢复、更不会启动本地调度器。
    CRON_JOBS_SYNC = "cron.jobs.sync"
    CRON_COMMAND_ACK = "cron.command.ack"
    CRON_RUN_NOW_ACK = "cron.run_now.ack"

    # Gateway owns voice task records; AgentServer returns execution facts/files.
    VOICE_TASK_CHECKPOINT_ACK = "voice.task.checkpoint.ack"
    VOICE_TASK_FILES = "voice.task.files"

    # HarmonyOS TUI DevEco bootstrap（Phase 3：用户态在目标 AgentServer 注入目录执行）
    HARMONYOS_PROJECT_INIT = "harmonyos.project_init"
    HARMONYOS_DEV_INIT = "harmonyos.dev_init"

    TTS_SYNTHESIZE = "tts.synthesize"

    AGENTS_LIST = "agents.list"
    AGENTS_GET = "agents.get"
    AGENTS_CREATE = "agents.create"
    AGENTS_UPDATE = "agents.update"
    AGENTS_DELETE = "agents.delete"
    AGENTS_ENABLE = "agents.enable"
    AGENTS_DISABLE = "agents.disable"
    AGENTS_TOOLS_LIST = "agents.tools_list"
    AGENT_SWITCH = "3rdagent.switch"
    AGENT_LIST = "3rdagent.list"

    # Unified asset publishing; dispatched by AgentServer independently of chats.
    ASSETS_PUBLISH_DESCRIBE = "assets.publish.describe"
    ASSETS_PUBLISH_PREPARE = "assets.publish.prepare"
    ASSETS_PUBLISH_COMMIT = "assets.publish.commit"
    ASSETS_PUBLISH_STATUS = "assets.publish.status"
    ASSETS_PUBLISH_RECORDS = "assets.publish.records"
    ASSETS_PUBLISH_LOCAL_STATUS = "assets.publish.local_status"

    # mcp management.
    MCP_LIST = "mcp.list"
    MCP_SHOW = "mcp.show"
    MCP_INSTALL = "mcp.install"
    MCP_UNINSTALL = "mcp.uninstall"
    MCP_CONNECT = "mcp.connect"
    MCP_WAIT_AUTH = "mcp.wait_auth"
    MCP_CANCEL_CONNECT = "mcp.cancel_connect"
    MCP_DISCONNECT = "mcp.disconnect"
    MCP_REGISTER_CUSTOM = "mcp.register_custom"
    MCP_DELETE_CUSTOM = "mcp.delete_custom"
    MCP_SAVE_CREDENTIALS = "mcp.save_credentials"

    SKILLS_MARKETPLACE_LIST = "skills.marketplace.list"
    SKILLS_LIST = "skills.list"
    SKILLS_INSTALLED = "skills.installed"
    SKILLS_GET = "skills.get"
    SKILLS_VERSIONS_LIST = "skills.versions.list"
    SKILLS_FILES_LIST = "skills.files.list"
    SKILLS_FILES_GET = "skills.files.get"
    SKILLS_REBUILD = "skills.rebuild"
    SKILLS_TOGGLE = "skills.toggle"
    # Per-workspace Skill visibility (team mode): the Skill entities themselves
    # live in exactly one global library, so who may see which Skill is metadata
    # stored next to a member / team workspace rather than a directory layout.
    SKILLS_VISIBILITY_GET = "skills.visibility.get"
    SKILLS_VISIBILITY_SET = "skills.visibility.set"
    SKILLS_VISIBILITY_UPDATE = "skills.visibility.update"
    SKILLS_INSTALL = "skills.install"
    SKILLS_PACK_MEMBER_INSTALL = "skills.pack_member.install"
    SKILLS_IMPORT_LOCAL = "skills.import_local"
    SKILLS_IMPORT_UPLOAD = "skills.import_upload"
    SKILLS_CREATE_FROM_KNOWLEDGE = "skills.create_from_knowledge"
    SKILLS_MARKETPLACE_ADD = "skills.marketplace.add"
    SKILLS_MARKETPLACE_REMOVE = "skills.marketplace.remove"
    SKILLS_MARKETPLACE_TOGGLE = "skills.marketplace.toggle"
    SKILLS_UNINSTALL = "skills.uninstall"
    SKILLS_ONLINE_SEARCH = "skills.online_search.search"
    SKILLS_ONLINE_SEARCH_INSTALL = "skills.online_search.install"
    SKILLS_SKILLNET_SEARCH = "skills.skillnet.search"
    SKILLS_SKILLNET_INSTALL = "skills.skillnet.install"
    SKILLS_SKILLNET_INSTALL_STATUS = "skills.skillnet.install_status"
    SKILLS_SKILLNET_EVALUATE = "skills.skillnet.evaluate"
    SKILLS_CLAWHUB_GET_TOKEN = "skills.clawhub.get_token"
    SKILLS_CLAWHUB_SET_TOKEN = "skills.clawhub.set_token"
    SKILLS_CLAWHUB_SEARCH = "skills.clawhub.search"
    SKILLS_CLAWHUB_DOWNLOAD = "skills.clawhub.download"
    SKILLS_TEAMSKILLS_HUB_INFO = "skills.teamskillshub.info"
    SKILLS_TEAMSKILLS_HUB_INIT = "skills.teamskillshub.init"
    SKILLS_TEAMSKILLS_HUB_VALIDATE = "skills.teamskillshub.validate"
    SKILLS_TEAMSKILLS_HUB_PACK = "skills.teamskillshub.pack"
    SKILLS_TEAMSKILLS_HUB_SEARCH = "skills.teamskillshub.search"
    SKILLS_SWARMSKILLS_HUB_RECOMMEND = "skills.swarmskillshub.recommend"
    SKILLS_TEAMSKILLS_HUB_INSTALL = "skills.teamskillshub.install"
    SKILLS_TEAMSKILLS_HUB_PUBLISH = "skills.teamskillshub.publish"
    SKILLS_TEAMSKILLS_HUB_DELETE = "skills.teamskillshub.delete"
    SKILLS_SWARMSKILLS_HUB_DETAIL = "skills.swarmskillshub.detail"
    SKILLS_RETRIEVAL_STATUS = "skills.retrieval.status"
    SKILLS_RETRIEVAL_INDEX_BUILD = "skills.retrieval.index_build"
    SKILLS_RETRIEVAL_INDEX_CANCEL = "skills.retrieval.index_cancel"
    SKILLS_RETRIEVAL_SEARCH = "skills.retrieval.search"
    SKILLS_RETRIEVAL_TREE = "skills.retrieval.tree"
    SKILLS_EVOLUTION_STATUS = "skills.evolution.status"
    SKILLS_EVOLUTION_GET = "skills.evolution.get"
    SKILLS_EVOLUTION_SAVE = "skills.evolution.save"

    # Skill Graph Web panel transport. The implementation is provided by
    # agent-core Symphony, while the public transport remains skill-domain API.
    SKILLS_GRAPH_BUILD = "skills.graph.build"
    SKILLS_GRAPH_STATUS = "skills.graph.status"
    SKILLS_GRAPH_GET = "skills.graph.get"
    SKILLS_GRAPH_CANCEL = "skills.graph.cancel"
    SKILLS_EXPERIENCE_LIST = "skills.experience.list"
    SKILLS_EXPERIENCE_REQUEST = "skills.experience.request"

    # Task (session) assets: named files referenceable with @name in the chat box.
    SESSION_ASSETS_LIST = "session.assets.list"
    SESSION_ASSETS_REGISTER = "session.assets.register"
    SESSION_ASSETS_RENAME = "session.assets.rename"

    # 技能同步（Server / Client 分离部署）。外部入口为 HTTP /skill-sync/*
    # （Bearer token 鉴权）。WS 侧仅保留 reload：web 进程 HTTP apply 落盘后的
    # 进程间通知（参数仅 applied 名称列表，纯 JSON）；diff / package / apply
    # 不注册 WS 路由（经 WS 调用会绕过 token 鉴权，且 package 泄漏临时目录、
    # apply 的 zip_bytes 无法过 JSON 序列化，均不可用）。
    SKILL_SYNC_DIFF = "skills.sync.diff"
    SKILL_SYNC_PACKAGE = "skills.sync.package"
    SKILL_SYNC_APPLY = "skills.sync.apply"
    SKILL_SYNC_RELOAD = "skills.sync.reload"

    PERSONAL_CONTEXT_RUNTIME_STATUS = "personal_context.runtime.status"
    PERSONAL_CONTEXT_RUNTIME_START_COLLECTION = (
        "personal_context.runtime.start_collection"
    )
    PERSONAL_CONTEXT_RUNTIME_STOP_COLLECTION = (
        "personal_context.runtime.stop_collection"
    )
    PERSONAL_CONTEXT_RUNTIME_START_AGENT_USE = (
        "personal_context.runtime.start_agent_use"
    )
    PERSONAL_CONTEXT_RUNTIME_STOP_AGENT_USE = "personal_context.runtime.stop_agent_use"
    PERSONAL_CONTEXT_RUNTIME_SET_MASTER_ENABLED = (
        "personal_context.runtime.set_master_enabled"
    )
    PERSONAL_CONTEXT_RUNTIME_GET_CONFIG = "personal_context.runtime.get_config"
    PERSONAL_CONTEXT_RUNTIME_PATCH_CONFIG = "personal_context.runtime.patch_config"
    PERSONAL_CONTEXT_RUNTIME_SELECT_MODEL = "personal_context.runtime.select_model"
    PERSONAL_CONTEXT_FETCH_LIST_SERVICES = "personal_context.fetch.list_services"
    PERSONAL_CONTEXT_FETCH_CREATE_SERVICE = "personal_context.fetch.create_service"
    PERSONAL_CONTEXT_FETCH_DELETE_SERVICE = "personal_context.fetch.delete_service"
    PERSONAL_CONTEXT_FETCH_PATCH_SERVICE = "personal_context.fetch.patch_service"
    PERSONAL_CONTEXT_FETCH_START_SERVICE = "personal_context.fetch.start_service"
    PERSONAL_CONTEXT_FETCH_STOP_SERVICE = "personal_context.fetch.stop_service"
    PERSONAL_CONTEXT_FETCH_RUN_ALL = "personal_context.fetch.run_all"
    PERSONAL_CONTEXT_FETCH_RUN_ONE = "personal_context.fetch.run_one"
    PERSONAL_CONTEXT_FETCH_STOP_RUN = "personal_context.fetch.stop_run"
    PERSONAL_CONTEXT_FETCH_GET_RUN_STATUS = "personal_context.fetch.get_run_status"
    PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS = (
        "personal_context.fetch.get_authorization_status"
    )
    PERSONAL_CONTEXT_FETCH_AUTHORIZE_PROVIDER = (
        "personal_context.fetch.authorize_provider"
    )
    PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH = "personal_context.context.stream_graph"
    PERSONAL_CONTEXT_CONTEXT_STREAM_TREE = "personal_context.context.stream_tree"
    PERSONAL_CONTEXT_CONTEXT_SEARCH_PAGES = "personal_context.context.search_pages"
    PERSONAL_CONTEXT_CONTEXT_GET_NODE = "personal_context.context.get_node"
    PERSONAL_CONTEXT_CONTEXT_GET_SOURCE = "personal_context.context.get_source"

    # Plugin management (reuses skills marketplace infrastructure)
    PLUGINS_LIST = "plugins.list"
    PLUGINS_INSTALL = "plugins.install"
    PLUGINS_UNINSTALL = "plugins.uninstall"
    PLUGINS_ENABLE = "plugins.enable"
    PLUGINS_DISABLE = "plugins.disable"
    PLUGINS_RELOAD = "plugins.reload"

    EXTENSIONS_LIST = "extensions.list"
    EXTENSIONS_IMPORT = "extensions.import"
    EXTENSIONS_DELETE = "extensions.delete"
    EXTENSIONS_TOGGLE = "extensions.toggle"

    # AgentGroup selection + agent_template / plugin package catalog RPCs.
    AGENT_GROUPS_LIST = "agent_groups.list"
    AGENT_GROUPS_SHOW = "agent_groups.show"
    AGENT_GROUPS_FILE_LIST = "agent_groups.file.list"
    AGENT_GROUPS_FILE_READ = "agent_groups.file.read"
    AGENT_GROUPS_CREATE = "agent_groups.create"
    AGENT_GROUPS_IMPORT_LOCAL = "agent_groups.import_local"
    AGENT_GROUPS_INSTALL = "agent_groups.install"
    AGENT_GROUPS_UNINSTALL = "agent_groups.uninstall"
    AGENT_TEMPLATES_LIST = "agent_templates.list"
    AGENT_TEMPLATES_SHOW = "agent_templates.show"
    AGENT_TEMPLATES_FILE_LIST = "agent_templates.file.list"
    AGENT_TEMPLATES_FILE_READ = "agent_templates.file.read"
    AGENT_TEMPLATES_CREATE = "agent_templates.create"
    AGENT_TEMPLATES_UPDATE = "agent_templates.update"
    AGENT_TEMPLATES_DELETE = "agent_templates.delete"
    AGENT_TEMPLATES_IMPORT_LOCAL = "agent_templates.import_local"
    AGENT_TEMPLATES_INSTALL = "agent_templates.install"
    AGENT_TEMPLATES_UNINSTALL = "agent_templates.uninstall"
    PLUGIN_PACKAGES_LIST = "plugin_packages.list"
    PLUGIN_PACKAGES_SHOW = "plugin_packages.show"
    PLUGIN_PACKAGES_CREATE = "plugin_packages.create"
    PLUGIN_PACKAGES_IMPORT_LOCAL = "plugin_packages.import_local"
    PLUGIN_PACKAGES_INSTALL = "plugin_packages.install"
    PLUGIN_PACKAGES_UNINSTALL = "plugin_packages.uninstall"

    HOOKS_LIST = "hooks.list"

    # 旧探活使用 health_check 命名空间。
    HEALTH_CHECK_GET_CONF = "health_check.get_conf"
    HEALTH_CHECK_SET_CONF = "health_check.set_conf"
    # Deprecated probe RPC aliases kept for clients upgrading across the rename.
    HEARTBEAT_GET_CONF = "heartbeat.get_conf"
    HEARTBEAT_SET_CONF = "heartbeat.set_conf"

    # Gateway -> AgentServer proxy for AgentServer-owned Heartbeat job operations.
    HEARTBEAT_JOB = "heartbeat.job"

    # 安全防护 permissions（与 Web ``register_method`` 同名，经 E2A → AgentServer 处理；owner_scopes 仅走 Web 直连）
    PERMISSIONS_TOOLS_GET = "permissions.tools.get"
    PERMISSIONS_TOOLS_SET = "permissions.tools.set"
    PERMISSIONS_TOOLS_UPDATE = "permissions.tools.update"
    PERMISSIONS_TOOLS_UPDATE_USER = "permissions.tools.update_user"
    PERMISSIONS_TOOLS_DELETE = "permissions.tools.delete"
    PERMISSIONS_RULES_GET = "permissions.rules.get"
    PERMISSIONS_RULES_CREATE = "permissions.rules.create"
    PERMISSIONS_RULES_UPDATE = "permissions.rules.update"
    PERMISSIONS_RULES_DELETE = "permissions.rules.delete"
    PERMISSIONS_APPROVAL_OVERRIDES_GET = "permissions.approval_overrides.get"
    PERMISSIONS_APPROVAL_OVERRIDES_DELETE = "permissions.approval_overrides.delete"
    PERMISSIONS_OWNER_SCOPES_GET = "permissions.owner_scopes.get"
    PERMISSIONS_OWNER_SCOPES_SET = "permissions.owner_scopes.set"

    MEMORY_FORBIDDEN_GET = "memory.forbidden.get"
    MEMORY_FORBIDDEN_SET = "memory.forbidden.set"

    # A4P 授权桥接（Web User Authorizer → AgentServer A4PServer）
    A4P_AUTHORIZATION_REPREPARE = "a4p.authorization.reprepare"
    A4P_AUTHORIZATION_COMPLETE = "a4p.authorization.complete"
    A4P_AUTHORIZATION_REJECT = "a4p.authorization.reject"
    A4P_AUTHORIZATION_PENDING = "a4p.authorization.pending"
    A4P_CONFIG_GET = "a4p.config.get"
    A4P_CONFIG_UPDATE = "a4p.config.update"

    CHANNEL_FEISHU_GET_CONF = "channel.feishu.get_conf"
    CHANNEL_FEISHU_SET_CONF = "channel.feishu.set_conf"

    CHANNEL_XIAOYI_GET_CONF = "channel.xiaoyi.get_conf"
    CHANNEL_XIAOYI_SET_CONF = "channel.xiaoyi.set_conf"

    CHANNEL_TELEGRAM_GET_CONF = "channel.telegram.get_conf"
    CHANNEL_TELEGRAM_SET_CONF = "channel.telegram.set_conf"
    CHANNEL_SLACK_GET_CONF = "channel.slack.get_conf"
    CHANNEL_SLACK_SET_CONF = "channel.slack.set_conf"
    CHANNEL_DINGTALK_GET_CONF = "channel.dingtalk.get_conf"
    CHANNEL_DINGTALK_SET_CONF = "channel.dingtalk.set_conf"

    CHANNEL_WHATSAPP_GET_CONF = "channel.whatsapp.get_conf"
    CHANNEL_WHATSAPP_SET_CONF = "channel.whatsapp.set_conf"
    CHANNEL_WECHAT_GET_CONF = "channel.wechat.get_conf"
    CHANNEL_WECHAT_SET_CONF = "channel.wechat.set_conf"
    CHANNEL_WECHAT_GET_LOGIN_UI = "channel.wechat.get_login_ui"
    CHANNEL_WECHAT_UNBIND = "channel.wechat.unbind"

    UPDATER_GET_STATUS = "updater.get_status"
    UPDATER_CHECK = "updater.check"
    UPDATER_DOWNLOAD = "updater.download"
    UPDATER_GET_CONF = "updater.get_conf"
    UPDATER_SET_CONF = "updater.set_conf"

    TEAM_SNAPSHOT = "team.snapshot"
    TEAM_HISTORY_GET = "team.history.get"
    TEAM_MEMBERS_GET = "team.members.get"
    TEAM_MQ_PUBLISH = "team.mq.publish"
    TEAM_LIST = "team.list"
    ORG_SNAPSHOT = "org.snapshot"

    # Harness package management
    HARNESS_PACKAGES_GET = "harness.packages.get"
    HARNESS_PACKAGES_SCAN = "harness.packages.scan"
    HARNESS_PACKAGES_ACTIVATE = "harness.packages.activate"
    HARNESS_PACKAGES_DEACTIVATE = "harness.packages.deactivate"
    HARNESS_PACKAGES_DELETE = "harness.packages.delete"
    HARNESS_PACKAGES_IMPORT = "harness.packages.import"
    HARNESS_PACKAGES_EXPORT = "harness.packages.export"

    # RSI 优化平台（web → gateway → agentserver 全链路，web 契约 v0.3 §4）
    RSI_DATASET_VALIDATE = "rsi.dataset.validate"
    RSI_TASK_CREATE = "rsi.task.create"
    RSI_TASK_LIST = "rsi.task.list"
    RSI_TASK_GET = "rsi.task.get"
    RSI_TASK_DELETE = "rsi.task.delete"
    RSI_TRAINING_START = "rsi.training.start"
    RSI_TRAINING_PAUSE = "rsi.training.pause"
    RSI_TRAINING_RESUME = "rsi.training.resume"
    RSI_TRAINING_TERMINATE = "rsi.training.terminate"
    RSI_REPORT_GET = "rsi.report.get"
    RSI_USAGE_GET = "rsi.usage.get"
    RSI_ARTIFACT_DOWNLOAD = "rsi.artifact.download"
    RSI_ARTIFACT_FILES_LIST = "rsi.artifact.files.list"
    RSI_ARTIFACT_FILES_GET = "rsi.artifact.files.get"
    RSI_TREE_GET = "rsi.tree.get"
    RSI_HARNESS_INSTALL = "rsi.harness.install"
    RSI_HARNESS_VERSIONS_LIST = "rsi.harness.versions.list"
    RSI_HARNESS_ROLLBACK = "rsi.harness.rollback"

    # Schedule task management
    SCHEDULE_CHECK_CONFIG = "schedule.check_config"
    SCHEDULE_UPDATE_CONFIG = "schedule.update_config"
    SCHEDULE_CREATE = "schedule.create"
    SCHEDULE_RUN = "schedule.run"
    SCHEDULE_LIST = "schedule.list"
    SCHEDULE_STATUS = "schedule.status"
    SCHEDULE_LOGS = "schedule.logs"
    SCHEDULE_CANCEL = "schedule.cancel"
    SCHEDULE_DELETE = "schedule.delete"
    ISSUE_WATCH_ONCE = "issue.watch_once"
    ISSUE_STATE_LIST = "issue.state.list"
    ISSUE_DELETE = "issue.delete"
    ISSUE_MATRIX = "issue.matrix"


class EventType(Enum):
    SESSION_ARCHIVED = "session.archived"
    SESSION_UNARCHIVED = "session.unarchived"
    SESSION_DELETED = "session.deleted"
    SESSION_LIFECYCLE_UPDATED = "session.lifecycle.updated"
    PROJECT_LIFECYCLE_UPDATED = "project.lifecycle.updated"
    # 项目移除(软删除)/恢复：其会话与定时任务的可见性随之变化，
    # 其他端必须据此刷新工作区、归档页与定时任务列表。
    PROJECT_REMOVED = "project.removed"
    PROJECT_RESTORED = "project.restored"
    CONNECTION_ACK = "connection.ack"
    HELLO = "hello"
    CHAT_DELTA = "chat.delta"
    CHAT_REASONING = "chat.reasoning"
    CHAT_USAGE_METADATA = "chat.usage_metadata"
    CHAT_USAGE_SUMMARY = "chat.usage_summary"
    CHAT_FINAL = "chat.final"
    CHAT_RETRACT = "chat.retract"
    CHAT_MEDIA = "chat.media"
    CHAT_FILE = "chat.file"
    CHAT_TOOL_CALL = "chat.tool_call"
    CHAT_TOOL_UPDATE = "chat.tool_update"
    CHAT_TOOL_RESULT = "chat.tool_result"
    CHAT_SYMPHONY_STATUS = "chat.symphony_status"
    CHAT_MESSAGE_UPDATED = "chat.message_updated"
    CONTEXT_USAGE = "context.usage"
    TODO_UPDATED = "todo.updated"
    CHAT_PROCESSING_STATUS = "chat.processing_status"
    CHAT_ERROR = "chat.error"
    CHAT_INTERRUPT_RESULT = "chat.interrupt_result"
    CHAT_EVOLUTION_STATUS = "chat.evolution_status"
    CHAT_SUBTASK_UPDATE = "chat.subtask_update"
    CHAT_SUBAGENT_ACTIVITY = "chat.subagent_activity"
    CHAT_ASK_USER_QUESTION = "chat.ask_user_question"
    PLAN_APPROVAL_REQUIRED = "plan.approval_required"
    CHAT_SESSION_RESULT = "chat.session_result"
    GOAL_SNAPSHOT = "goal.snapshot"
    GOAL_UPDATED = "goal.updated"
    RUNTIME_ACCEPTED = "runtime.accepted"
    EXECUTION_ERROR = "execution.error"
    TEAM_MEMBER = "team.member"
    TEAM_TASK = "team.task"
    TEAM_MESSAGE = "team.message"
    WORKFLOW_UPDATED = "workflow.updated"
    DESIGNER_RUN_UPDATED = "designer.run.updated"
    DESIGNER_NODE_UPDATED = "designer.node.updated"
    DESIGNER_GRAPH_UPDATED = "designer.graph.updated"
    DESIGNER_LEADER_ACTIVITY = "designer.leader.activity"
    # 旧探活结果通过 health_check.relay 发送。
    # 新心跳任务(heartbeat.job.*)不使用 relay 事件,结果通过普通 chat.send 进入原会话。
    HEALTH_CHECK_RELAY = "health_check.relay"
    # Deprecated source-level alias. Legacy wire frames are normalized by
    # _missing_ so every downstream channel sees HEALTH_CHECK_RELAY.
    HEARTBEAT_RELAY = "health_check.relay"
    HISTORY_GET = "history.message"
    PROACTIVE_RECOMMENDATION = "proactive_recommendation"

    @classmethod
    def _missing_(cls, value):
        if value == "heartbeat.relay":
            return cls.HEALTH_CHECK_RELAY
        return None


class Mode(Enum):
    AGENT = "agent"
    # 历史值：plan / fast 已合并为 agent，保留以兼容旧序列化数据的反解析。
    AGENT_PLAN = "agent.plan"
    AGENT_FAST = "agent.fast"
    CODE_PLAN = "code.plan"
    CODE_NORMAL = "code.normal"
    CODE_TEAM = "code.team"
    TEAM = "team"
    TEAM_PLAN_NORMAL = "team.plan.normal"
    TEAM_PLAN_CODE = "team.plan.code"
    # 新三段命名 canonical（P2 引入；旧成员保留以兼容历史持久化反解析）。
    AGENT_WORK_NORMAL = "agent.work.normal"
    AGENT_WORK_PLAN = "agent.work.plan"
    AGENT_CODE_NORMAL = "agent.code.normal"
    AGENT_CODE_PLAN = "agent.code.plan"
    TEAM_WORK_NORMAL = "team.work.normal"
    TEAM_WORK_PLAN = "team.work.plan"
    TEAM_CODE_NORMAL = "team.code.normal"
    TEAM_CODE_PLAN = "team.code.plan"

    @classmethod
    def from_raw(cls, raw_mode: Any, default: "Mode | None" = None) -> "Mode":
        """解析 mode：新 canonical 原样返回，旧 canonical 静默映射到新 canonical。

        单一逻辑路径——不再维护手写白名单：``deprecate_mode`` 对新 canonical
        原样返回（不在 :data:`DEPRECATION_MAP` 里），对旧 canonical 映射到新串，
        对未知串原样返回后由 ``cls(...)`` 抛 ``ValueError`` 兜底到 ``fallback``。
        裸 ``plan`` / ``fast`` 与 CLI ``MODE_ALIASES`` 同语义，直接落到
        :attr:`AGENT_WORK_NORMAL`，不绕 ``agent.fast`` 中间步。
        """
        fallback = default or cls.AGENT_WORK_NORMAL
        if isinstance(raw_mode, Mode):
            raw_mode = raw_mode.value
        if not isinstance(raw_mode, str):
            return fallback
        normalized = raw_mode.strip().lower()
        if not normalized:
            return fallback
        if normalized in ("plan", "fast"):
            logger.debug("Mode.from_raw: bare '%s' -> AGENT_WORK_NORMAL", normalized)
            return cls.AGENT_WORK_NORMAL
        new_mode_str = deprecate_mode(normalized)
        try:
            mode = cls(new_mode_str)
        except ValueError:
            logger.warning(
                "Mode.from_raw: 无法识别的 mode=%r (deprecate 后=%r)，兜底 %s",
                raw_mode, new_mode_str, fallback.value,
=======
    ``openjiuwen-protocol`` owns the enum; 这里只为协议包尚未发布的成员开一个缺口，
    待协议包提供同名同值成员后本函数与调用一并删除。
    """
    existing = enum_cls.__members__.get(name)
    if existing is not None:
        if existing.value != value:
            raise RuntimeError(
                f"{enum_cls.__name__}.{name} is {existing.value!r}, expected {value!r}"
>>>>>>> origin/develop
            )
        return
    if any(item.value == value for item in enum_cls):
        raise RuntimeError(f"{enum_cls.__name__} value {value!r} is already registered")
    # Enum exposes no public API for adding a member, so extending the pinned
    # protocol enum requires its internal registries. Keep those accesses here.
    member = object.__new__(enum_cls)
    member._name_ = name  # pylint: disable=protected-access
    member._value_ = value  # pylint: disable=protected-access
    type.__setattr__(enum_cls, name, member)
    enum_cls._member_map_[name] = member  # pylint: disable=protected-access
    enum_cls._value2member_map_[value] = member  # pylint: disable=protected-access
    enum_cls._member_names_.append(name)  # pylint: disable=protected-access


def _ensure_req_method(name: str, value: str) -> None:
    _ensure_protocol_member(ReqMethod, name, value)


# Published release methods required by JiuwenMate and its existing callers.
_ensure_req_method("JIUWENMATE_FEISHU_INBOUND", "jiuwenmate.feishu.inbound")
_ensure_req_method("JIUWENMATE_DINGTALK_INBOUND", "jiuwenmate.dingtalk.inbound")
_ensure_req_method("JIUWENMATE_DINGTALK_BINDING_CHECK", "jiuwenmate.dingtalk.binding_check")
_ensure_req_method("JIUWENMATE_WELINK_INBOUND", "jiuwenmate.welink.inbound")
_ensure_req_method("JIUWENMATE_CONNECTION_ATTACH", "jiuwenmate.connection.attach")
_ensure_req_method("JIUWENMATE_COLLEAGUE_CONNECTIONS", "jiuwenmate.colleague.connections")
_ensure_req_method("JIUWENMATE_COLLEAGUE_REMOVE", "jiuwenmate.colleague.remove")
_ensure_req_method("JIUWENMATE_FEISHU_PREPARE", "jiuwenmate.feishu.prepare")
_ensure_req_method("JIUWENMATE_DELIVERY_ACK", "jiuwenmate.delivery.ack")
_ensure_req_method("JIUWENMATE_THINKING_NOTICE_ACK", "jiuwenmate.thinking_notice.ack")
_ensure_req_method("JIUWENMATE_GROUP_RESPONSE", "jiuwenmate.group.response")
_ensure_req_method("GROUP_EXECUTION_LIST", "group.execution.list")
_ensure_req_method("GROUP_EXECUTION_GET", "group.execution.get")
_ensure_req_method("GROUP_FILE_RENAME", "group.file.rename")
_ensure_req_method("GROUP_FILE_DELETE", "group.file.delete")
_ensure_req_method("GROUP_HISTORY_CLEAR", "group.history.clear")
_ensure_req_method("MODELS_REFRESH_STATUS", "models.refresh_status")
_ensure_req_method("VOICE_TASK_PENDING_INTERRUPT", "voice.task.pending_interrupt")
_ensure_req_method("VOICE_SESSION_RECEIPTS", "voice.session.receipts")
_ensure_req_method("JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT", "jiuwenmate.colleague.ensure_default")
_ensure_req_method("JIUWENMATE_COLLEAGUE_LIST", "jiuwenmate.colleague.list")
_ensure_req_method("JIUWENMATE_COLLEAGUE_GET", "jiuwenmate.colleague.get")
_ensure_req_method("JIUWENMATE_COLLEAGUE_CREATE", "jiuwenmate.colleague.create")
_ensure_req_method("JIUWENMATE_COLLEAGUE_SETTINGS_GET", "jiuwenmate.colleague.settings.get")
_ensure_req_method("JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE", "jiuwenmate.colleague.settings.update")
_ensure_req_method("JIUWENMATE_COLLEAGUE_PUBLISH", "jiuwenmate.colleague.publish")
_ensure_req_method("JIUWENMATE_COLLEAGUE_SET_ENABLED", "jiuwenmate.colleague.set_enabled")
_ensure_req_method("JIUWENMATE_COLLEAGUE_DELETE", "jiuwenmate.colleague.delete")
_ensure_req_method("JIUWENMATE_MODELS_LIST", "jiuwenmate.models.list")
_ensure_req_method("JIUWENMATE_CHAT_SEND", "jiuwenmate.chat.send")
_ensure_req_method("JIUWENMATE_CHAT_HISTORY", "jiuwenmate.chat.history")
_ensure_req_method("JIUWENMATE_ARTIFACT_LIST", "jiuwenmate.artifact.list")
_ensure_req_method("JIUWENMATE_REQUEST_GET", "jiuwenmate.request.get")
_ensure_req_method("JIUWENMATE_REQUEST_ABORT", "jiuwenmate.request.abort")
_ensure_req_method("JIUWENMATE_PERMISSION_DECIDE", "jiuwenmate.permission.decide")
_ensure_req_method("JIUWENMATE_SHARING_LIST", "jiuwenmate.sharing.list")
_ensure_req_method("JIUWENMATE_SHARING_DECIDE", "jiuwenmate.sharing.decide")
_ensure_req_method("JIUWENMATE_SHARING_REVOKE", "jiuwenmate.sharing.revoke")
_ensure_req_method("JIUWENMATE_CONVERSATION_LIST", "jiuwenmate.conversation.list")
_ensure_req_method("JIUWENMATE_CONVERSATION_RENAME", "jiuwenmate.conversation.rename")
_ensure_req_method("JIUWENMATE_CONVERSATION_PIN", "jiuwenmate.conversation.pin")
_ensure_req_method("JIUWENMATE_CONVERSATION_DELETE", "jiuwenmate.conversation.delete")
_ensure_req_method("JIUWENMATE_ACTIVITY_GET", "jiuwenmate.activity.get")
_ensure_req_method("JIUWENMATE_KNOWLEDGE_LIST", "jiuwenmate.knowledge.list")
_ensure_req_method("JIUWENMATE_SCHEDULE_LIST", "jiuwenmate.schedule.list")
_ensure_req_method("JIUWENMATE_SCHEDULE_CREATE", "jiuwenmate.schedule.create")
_ensure_req_method("JIUWENMATE_SCHEDULE_SET_ENABLED", "jiuwenmate.schedule.set_enabled")
_ensure_req_method("JIUWENMATE_SCHEDULE_DELETE", "jiuwenmate.schedule.delete")
_ensure_req_method("JIUWENMATE_SCHEDULE_UPDATE", "jiuwenmate.schedule.update")
_ensure_req_method("JIUWENMATE_SCHEDULE_RUN", "jiuwenmate.schedule.run")
_ensure_req_method("JIUWENMATE_SCHEDULE_HISTORY", "jiuwenmate.schedule.history")
_ensure_req_method("JIUWENMATE_CHANNEL_LIST", "jiuwenmate.channel.list")
_ensure_req_method("JIUWENMATE_CHANNEL_MESSAGES", "jiuwenmate.channel.messages")
_ensure_req_method("JIUWENMATE_CHANNEL_TRACE", "jiuwenmate.channel.trace")
_ensure_req_method("JIUWENMATE_WELINK_OFFICE_STATUS", "jiuwenmate.welink_office.status")
_ensure_req_method("JIUWENMATE_WELINK_OFFICE_CONNECT", "jiuwenmate.welink_office.connect")
_ensure_req_method("JIUWENMATE_WELINK_OFFICE_DISCONNECT", "jiuwenmate.welink_office.disconnect")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_STATUS", "jiuwenmate.agent_mail.status")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_AUTH_BEGIN", "jiuwenmate.agent_mail.auth.begin")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_AUTH_POLL", "jiuwenmate.agent_mail.auth.poll")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_AUTH_CANCEL", "jiuwenmate.agent_mail.auth.cancel")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_DISCONNECT", "jiuwenmate.agent_mail.disconnect")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_STATUS", "jiuwenmate.dingtalk_auth.status")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_BEGIN", "jiuwenmate.dingtalk_auth.begin")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_POLL", "jiuwenmate.dingtalk_auth.poll")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_CANCEL", "jiuwenmate.dingtalk_auth.cancel")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_DISCONNECT", "jiuwenmate.dingtalk_auth.disconnect")

_ensure_req_method("PROMPT_SUGGESTION_CANCEL", "prompt_suggestion.cancel")
_ensure_req_method("RECOMMENDATION", "recommendation.job")
# Dispatch compares this member on every request. The pinned protocol package
# does not define it, so a missing attribute fails chat and session creation
# before their own handlers run.
_ensure_req_method("MCP_OAUTH_START", "mcp.oauth.start")
# Trajectory reads are served by the AgentServer. The pinned protocol package
# does not define these members yet; registration leaves a matching upstream
# member in place once that package ships the same name and value.
_ensure_req_method("TRAJECTORY_SUBJECTS_LIST", "trajectory.subjects.list")
_ensure_req_method("TRAJECTORY_STREAM_FRAMES_GET", "trajectory.stream_frames.get")
_ensure_req_method("TRAJECTORY_SEQUENCES_GET", "trajectory.sequences.get")
_ensure_req_method("TRAJECTORY_CHECKPOINTS_GET", "trajectory.checkpoints.get")
_ensure_req_method("TRAJECTORY_USAGE_GET", "trajectory.usage.get")
_ensure_req_method("TRAJECTORY_SUBJECT_GET", "trajectory.subject.get")
_ensure_req_method("TRAJECTORY_RAW_GET", "trajectory.raw.get")
_ensure_req_method("TRAJECTORY_ARCHIVE_OPEN", "trajectory.archive.open")
_ensure_req_method("TRAJECTORY_BLOB_READ", "trajectory.blob.read")
_ensure_req_method("TRAJECTORY_BLOB_CLOSE", "trajectory.blob.close")
# Enterprise verify still registers these wire methods in HANDLERS / adapters,
# but the pinned openjiuwen-protocol revision does not ship them yet. Without
# ensure, importing dispatch crashes the runtime worker and HTTP :8766 never
# binds (readiness stays connection-refused).
_ensure_req_method("FILE_READ_UPLOAD_CHUNK", "file.read_upload_chunk")
_ensure_req_method("WORKSPACE_TREE", "workspace.tree")
_ensure_req_method("WORKSPACE_PREVIEW", "workspace.preview")
_ensure_req_method("WORKSPACE_USAGE", "workspace.usage")
_ensure_req_method("WORKSPACE_ENTRIES_DELETE", "workspace.entries.delete")
_ensure_req_method("SKILLS_SOURCE_PROVIDERS", "skills.source.providers")
_ensure_req_method("SKILLS_SOURCE_SEARCH", "skills.source.search")
_ensure_req_method("SKILLS_SOURCE_INSTALL", "skills.source.install")
_ensure_req_method("SKILLS_UPDATES_CHECK", "skills.updates.check")
_ensure_req_method("SKILLS_UPDATE", "skills.update")
_ensure_req_method("SKILLS_ENTERPRISE_LIST", "skills.enterprise.list")
_ensure_req_method("SKILLS_ENTERPRISE_INSTALL", "skills.enterprise.install")
_ensure_req_method("SKILLS_ENTERPRISE_UNINSTALL", "skills.enterprise.uninstall")
_ensure_req_method("SKILLS_ENTERPRISE_SOURCE_PROVIDERS", "skills.enterprise.source.providers")
_ensure_req_method("SKILLS_ENTERPRISE_SOURCE_SEARCH", "skills.enterprise.source.search")
_ensure_req_method("SKILLDEV_START", "skilldev.start")
_ensure_req_method("SKILLDEV_RESPOND", "skilldev.respond")
_ensure_req_method("SKILLDEV_STATUS", "skilldev.status")
_ensure_req_method("SKILLDEV_DOWNLOAD", "skilldev.download")
_ensure_req_method("SKILLDEV_CANCEL", "skilldev.cancel")
_ensure_req_method("SKILLDEV_FILE_LIST", "skilldev.file.list")
_ensure_req_method("SKILLDEV_FILE_READ", "skilldev.file.read")
# Removing a composer attachment chip should also drop the session-asset
# registry entry it created (see session_assets.py). The pinned protocol
# package does not define this member yet.
_ensure_req_method("SESSION_ASSETS_REMOVE", "session.assets.remove")
# Hub expert/equipment catalog served by AgentServer (dispatch.py / methods.py /
# http_invoke.py reference this member); 协议包当前 pin 尚未定义。
_ensure_req_method("HUB_EXPERT_PACKAGES_CATALOG", "hub.expert_packages.catalog")
# MCP Apps host bridge (io.modelcontextprotocol/ui); 协议包当前 pin 尚未定义。
_ensure_req_method("MCP_APP_LIST_TOOLS", "mcp_app.list_tools")
_ensure_req_method("MCP_APP_READ_RESOURCE", "mcp_app.read_resource")
_ensure_req_method("MCP_APP_CALL_TOOL", "mcp_app.call_tool")
# The Gateway forwards an event only when the protocol EventType knows it; the
# pinned protocol package does not define this one yet.
_ensure_protocol_member(EventType, "CHAT_CLIENT_TOOL_REQUEST", "chat.client_tool_request")
# Client attachments (e.g. the Office add-in task pane): a client attaches the
# resource it edits to a conversation, renews the attachment's lease, and reads
# the other attachments and their conversations. The pinned protocol package
# does not define these members yet.
_ensure_req_method("CLIENT_ATTACH", "client.attach")
_ensure_req_method("CLIENT_RENEW", "client.renew")
_ensure_req_method("CLIENT_LIST", "client.list")
_ensure_req_method("CLIENT_CONVERSATION_MESSAGES", "client.conversation.messages")
_ensure_req_method("CLIENT_CONVERSATIONS", "client.conversations")
_ensure_protocol_member(EventType, "CLIENT_ATTACHMENTS_CHANGED", "client.attachments_changed")

_ensure_req_method("VOICE_SESSION_RECEIPTS", "voice.session.receipts")
_ensure_req_method("VOICE_TASK_PENDING_INTERRUPT", "voice.task.pending_interrupt")

__all__ = [
    "PROMPT_SUGGESTION_READY_EVENT",
    "SESSION_REQUEST_COMPLETED_EVENT",
    "TEAM_ATTACH_ONLY_PARAM",
    "TOPIC_STARTERS_CONTROL_CHANGED_EVENT",
    "TOPIC_STARTERS_READY_EVENT",
    "EventType",
    "Message",
    "Mode",
    "ReqMethod",
]
