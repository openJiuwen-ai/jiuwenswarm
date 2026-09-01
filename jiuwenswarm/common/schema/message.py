# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""统一消息模型."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from jiuwenswarm.common.mode_matrix import deprecate_mode

logger = logging.getLogger(__name__)


class ReqMethod(Enum):
    INITIALIZE = "initialize"
    ACP_TOOL_RESPONSE = "acp.tool_response"

    CHAT_SEND = "chat.send"
    CHAT_RESUME = "chat.resume"
    CHAT_CANCEL = "chat.interrupt"
    CHAT_ANSWER = "chat.user_answer"
    CHAT_SWARMFLOW_REPLY = "chat.swarmflow_reply"
    SSH_RELAY = "ssh.relay"
    HISTORY_GET = "history.get"
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

    CONFIG_GET = "config.get"
    CONFIG_SET = "config.set"
    CONFIG_SAVE_ALL = "config.save_all"
    CONFIG_VALIDATE_MODEL = "config.validate_model"
    MODELS_LIST = "models.list"
    MODELS_REPLACE_ALL = "models.replace_all"
    MODELS_VALIDATE = "models.validate"
    LOCALE_GET_CONF = "locale.get_conf"
    LOCALE_SET_CONF = "locale.set_conf"
    CHANNEL_GET = "channel.get"

    SESSION_LIST = "session.list"
    SESSION_GET_METADATA = "session.get_metadata"
    SESSION_PIN = "session.pin"
    SESSION_COLOR_SET = "session.color_set"
    SESSION_PREVIEW = "session.preview"
    SESSION_CREATE = "session.create"
    SESSION_SWITCH = "session.switch"
    SESSION_DELETE = "session.delete"
    SESSION_KVC_PREPARE = "session.kvc.prepare"
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
    PROJECT_REMOVE = "project.remove"
    PROJECT_RESTORE = "project.restore"
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

    PROACTIVE_TICK = "proactive.tick"  # Trigger proactive recommendation tick (from Cron)
    COMMAND_GOAL = "command.goal"
    COMMANDS_LIST = "commands.list"

    FILES_LIST = "files.list"
    FILES_GET = "files.get"

    # 媒体/文档附件（Phase 2 WorkspaceFileAdapter）
    MEDIA_PERSIST = "media.persist"
    DOCUMENT_PERSIST = "document.persist"
    DOCUMENT_FORMATS = "document.formats"
    # chat.send 上行外部 url 文件导入（Phase 2：AgentServer 下载落盘注入目录，Gateway 不落盘）
    FILE_IMPORT_URL = "file.import_url"
    # 分块上传：用于 AgentOS 多用户场景的大文件，避免单个 E2A WebSocket 帧超过限制。
    FILE_UPLOAD_CHUNK = "file.upload_chunk"

    # IM 平台附件落盘（Phase 3：Gateway 下载字节后经 base64 交给 AgentServer
    # 落盘至其注入目录的 <平台>_files/downloads/，Gateway 不直写用户目录）
    IM_FILE_PERSIST = "im.file_persist"

    # Gateway cron 单源的只读内存快照。AgentServer 只消费该快照供本轮
    # cron 工具查询/更新，不持久化、不恢复、更不会启动本地调度器。
    CRON_JOBS_SYNC = "cron.jobs.sync"
    CRON_COMMAND_ACK = "cron.command.ack"
    CRON_RUN_NOW_ACK = "cron.run_now.ack"

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

    # mcp management.
    MCP_LIST = "mcp.list"
    MCP_SHOW = "mcp.show"
    MCP_CONNECT = "mcp.connect"
    MCP_WAIT_AUTH = "mcp.wait_auth"
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
    SKILLS_IMPORT_LOCAL = "skills.import_local"
    SKILLS_IMPORT_UPLOAD = "skills.import_upload"
    SKILLS_CREATE_FROM_KNOWLEDGE = "skills.create_from_knowledge"
    SKILLS_MARKETPLACE_ADD = "skills.marketplace.add"
    SKILLS_MARKETPLACE_REMOVE = "skills.marketplace.remove"
    SKILLS_MARKETPLACE_TOGGLE = "skills.marketplace.toggle"
    SKILLS_UNINSTALL = "skills.uninstall"
    SKILLS_ONLINE_SEARCH = "skills.online_search.search"
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

    # Harness package management
    HARNESS_PACKAGES_GET = "harness.packages.get"
    HARNESS_PACKAGES_SCAN = "harness.packages.scan"
    HARNESS_PACKAGES_ACTIVATE = "harness.packages.activate"
    HARNESS_PACKAGES_DEACTIVATE = "harness.packages.deactivate"
    HARNESS_PACKAGES_DELETE = "harness.packages.delete"
    HARNESS_PACKAGES_IMPORT = "harness.packages.import"
    HARNESS_PACKAGES_EXPORT = "harness.packages.export"

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
            )
            return fallback
        if new_mode_str != normalized:
            logger.debug(
                "Mode.from_raw: '%s' -> '%s' (legacy canonical 静默迁移)",
                normalized, new_mode_str,
            )
        return mode

    def to_runtime_mode(self) -> str:
        """输出 runtime mode 值；历史 agent.plan / agent.fast 归一为 agent。

        新枚举原样返回（canonical 串即 runtime 串）。旧枚举按历史语义归一为
        合并后的 ``agent`` / 自身 canonical 串。
        """
        if self in (Mode.AGENT_PLAN, Mode.AGENT_FAST):
            return Mode.AGENT.value
        return self.value


@dataclass
class Message:
    """统一消息结构."""
    id: str
    type: Literal["req", "res", "event"]
    channel_id: str
    session_id: str | None
    params: dict
    timestamp: float
    ok: bool
    provider: str | None = None
    chat_id: str | None = None
    user_id: str | None = None
    bot_id: str | None = None  # 已弃用，请使用 app_id + agent_ref 替代
    app_id: str | None = None  # V2: 应用实例标识，从 bot_id 拆出
    agent_ref: Any = None      # V2: AgentRef(mode, id)，后端智能体标识
    payload: dict | None = None
    req_method: ReqMethod | None = None
    event_type: EventType | None = None
    # 与 Mode.from_raw 的 fallback 对齐：客户端不传 mode 时落到 canonical
    # ``agent.work.normal``，避免 schema 默认值与 from_raw 不一致导致
    # Message.mode 在两条入口路径下产生不同默认值。旧 ``Mode.AGENT`` 仍由
    # from_raw("agent") 经 DEPRECATION_MAP 映射到 AGENT_WORK_NORMAL，故不会
    # 丢失旧 canonical 的反解析能力。
    mode: Mode = Mode.AGENT_WORK_NORMAL
    is_stream: bool = False
    stream_seq: int | None = None
    stream_id: str | None = None
    metadata: dict[str, Any] | None = None
    group_digital_avatar: bool = False
    enable_memory: bool | None = None
    enable_streaming: bool = True
