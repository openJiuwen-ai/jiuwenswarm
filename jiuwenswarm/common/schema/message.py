# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""统一消息模型."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


class ReqMethod(Enum):
    INITIALIZE = "initialize"
    ACP_TOOL_RESPONSE = "acp.tool_response"

    CHAT_SEND = "chat.send"
    CHAT_RESUME = "chat.resume"
    CHAT_CANCEL = "chat.interrupt"
    CHAT_ANSWER = "chat.user_answer"
    CHAT_SWARMFLOW_REPLY = "chat.swarmflow_reply"
    FILE_TRANSFER_START = "file.transfer.start"
    FILE_TRANSFER_CHUNK = "file.transfer.chunk"
    FILE_TRANSFER_COMPLETE = "file.transfer.complete"
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
    CHANNEL_GET = "channel.get"

    SESSION_LIST = "session.list"
    SESSION_CREATE = "session.create"
    SESSION_SWITCH = "session.switch"
    SESSION_DELETE = "session.delete"
    SESSION_RENAME = "session.rename"
    SESSION_FORK = "session.fork"
    SESSION_REWIND = "session.rewind"
    SESSION_REWIND_AND_RESTORE = "session.rewind_and_restore"
    SESSION_REWIND_CONTEXT = "session.rewind_context"
    SESSION_REWIND_COMPACT = "session.rewind_compact"
    SESSION_RESTORE_FILES = "session.restore_files"
    HISTORY_LIST_TURNS = "history.list_turns"
    TEAM_TEMPLATES_LIST = "team.templates.list"
    TEAM_BINDINGS_LIST = "team.bindings.list"
    TEAM_BINDING_CREATE = "team.binding.create"
    TEAM_BINDING_GENERATE = "team.binding.generate"
    TEAM_SESSION_BIND = "team.session.bind"
    TEAM_DELETE = "team.delete"
    TEAM_SESSION_RESET = "team.session.reset"
    TEAM_RUNTIME_DISSOLVE = "team.runtime.dissolve"

    PATH_GET = "path.get"
    PATH_SET = "path.set"

    BROWSER_RUNTIME_RESTART = "browser.runtime_restart"

    CONFIG_CACHE_CLEAR = "config.cache_clear"
    AGENT_RELOAD_CONFIG = "agent.reload_config"
    SYNC_AGENTS_CONFIGS = "sync_agents_configs"
    AGENT_PREWARM_SYNC = "agent.prewarm.sync"
    LOGGING_SET = "logging.set"

    MEMORY_COMPUTE = "memory.compute"

    PROACTIVE_TICK = "proactive.tick"  # Trigger proactive recommendation tick (from Cron)
    COMMAND_GOAL = "command.goal"

    FILES_LIST = "files.list"
    FILES_GET = "files.get"
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

    SKILLS_MARKETPLACE_LIST = "skills.marketplace.list"
    SKILLS_LIST = "skills.list"
    SKILLS_INSTALLED = "skills.installed"
    SKILLS_GET = "skills.get"
    SKILLS_TOGGLE = "skills.toggle"
    SKILLS_INSTALL = "skills.install"
    SKILLS_IMPORT_LOCAL = "skills.import_local"
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
    SKILLS_TEAMSKILLS_HUB_INSTALL = "skills.teamskillshub.install"
    SKILLS_TEAMSKILLS_HUB_PUBLISH = "skills.teamskillshub.publish"
    SKILLS_TEAMSKILLS_HUB_DELETE = "skills.teamskillshub.delete"
    SKILLS_SOURCE_PROVIDERS = "skills.source.providers"
    SKILLS_SOURCE_SEARCH = "skills.source.search"
    SKILLS_SOURCE_INSTALL = "skills.source.install"
    SKILLS_UPDATES_CHECK = "skills.updates.check"
    SKILLS_UPDATE = "skills.update"
    SKILLS_RETRIEVAL_STATUS = "skills.retrieval.status"
    SKILLS_RETRIEVAL_INDEX_BUILD = "skills.retrieval.index_build"
    SKILLS_RETRIEVAL_INDEX_CANCEL = "skills.retrieval.index_cancel"
    SKILLS_RETRIEVAL_SEARCH = "skills.retrieval.search"
    SKILLS_RETRIEVAL_TREE = "skills.retrieval.tree"
    SKILLS_EVOLUTION_STATUS = "skills.evolution.status"
    SKILLS_EVOLUTION_GET = "skills.evolution.get"
    SKILLS_EVOLUTION_SAVE = "skills.evolution.save"
    SKILLS_EVOLUTION_ARCHIVES = "skills.evolution.archives"
    SKILLS_EVOLUTION_ROLLBACK = "skills.evolution.rollback"
    SKILLS_EVOLUTION_REBUILD = "skills.evolution.rebuild"
    SKILLS_ENTERPRISE_LIST = "skills.enterprise.list"
    SKILLS_ENTERPRISE_INSTALL = "skills.enterprise.install"
    SKILLS_ENTERPRISE_UNINSTALL = "skills.enterprise.uninstall"
    SKILLS_ENTERPRISE_SOURCE_PROVIDERS = "skills.enterprise.source.providers"
    SKILLS_ENTERPRISE_SOURCE_SEARCH = "skills.enterprise.source.search"

    SYMPHONY_BUILD_SCORE = "symphony.build_score"
    SYMPHONY_PAUSE_BUILD = "symphony.pause_build"
    SYMPHONY_SCORE_STATUS = "symphony.score_status"
    SYMPHONY_GRAPH = "symphony.graph"
    SYMPHONY_PLAN = "symphony.plan"

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

    HOOKS_LIST = "hooks.list"

    HEARTBEAT_GET_CONF = "heartbeat.get_conf"
    HEARTBEAT_SET_CONF = "heartbeat.set_conf"

    # 安全防护 permissions（与 Web ``register_method`` 同名，经 E2A → AgentServer 处理；owner_scopes 仅走 Web 直连）
    PERMISSIONS_ENABLED_GET = "permissions.enabled.get"
    PERMISSIONS_ENABLED_SET = "permissions.enabled.set"
    PERMISSIONS_TOOLS_GET = "permissions.tools.get"
    PERMISSIONS_TOOLS_LIST = "permissions.tools.list"
    PERMISSIONS_TOOLS_SET = "permissions.tools.set"
    PERMISSIONS_TOOLS_UPDATE = "permissions.tools.update"
    PERMISSIONS_TOOLS_DELETE = "permissions.tools.delete"
    PERMISSIONS_RULES_GET = "permissions.rules.get"
    PERMISSIONS_RULES_CREATE = "permissions.rules.create"
    PERMISSIONS_RULES_UPDATE = "permissions.rules.update"
    PERMISSIONS_RULES_DELETE = "permissions.rules.delete"
    PERMISSIONS_APPROVAL_OVERRIDES_GET = "permissions.approval_overrides.get"
    PERMISSIONS_APPROVAL_OVERRIDES_DELETE = "permissions.approval_overrides.delete"
    PERMISSIONS_WORKSPACE_ENABLE_GET = "permissions.file_guard.workspace.rw_enabled.get"
    PERMISSIONS_WORKSPACE_ENABLE_SET = "permissions.file_guard.workspace.rw_enabled.set"
    PERMISSIONS_WORKSPACE_ACCESS_GET = "permissions.file_guard.workspace.access.get"
    PERMISSIONS_WORKSPACE_ACCESS_SET = "permissions.file_guard.workspace.access.set"

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
    TEAM_TASKS_DEPENDENCIES = "team.tasks.dependencies"

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
    TASK_START = "task.start"
    TASK_UPDATE = "task.update"
    TASK_COMPLETE = "task.complete"
    CHAT_PROCESSING_STATUS = "chat.processing_status"
    CHAT_ERROR = "chat.error"
    CHAT_INTERRUPT_RESULT = "chat.interrupt_result"
    CHAT_EVOLUTION_STATUS = "chat.evolution_status"
    CHAT_SUBTASK_UPDATE = "chat.subtask_update"
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
    HEARTBEAT_RELAY = "heartbeat.relay"
    HISTORY_GET = "history.message"
    PROACTIVE_RECOMMENDATION = "proactive_recommendation"
    SYNC_AGENTS_CONFIGS_RESULT = "sync_agents_configs.result"


class Mode(Enum):
    AGENT = "agent"
    # 历史值：plan / fast 已合并为 agent，保留以兼容旧序列化数据的反解析。
    AGENT_PLAN = "agent.plan"
    AGENT_FAST = "agent.fast"
    CODE_PLAN = "code.plan"
    CODE_NORMAL = "code.normal"
    CODE_TEAM = "code.team"
    TEAM = "team"

    @classmethod
    def from_raw(cls, raw_mode: Any, default: "Mode | None" = None) -> "Mode":
        """解析 mode。plan / fast 已合并：agent.plan / agent.fast 归一为 agent。"""
        fallback = default or cls.AGENT
        if isinstance(raw_mode, Mode):
            # 历史枚举成员归一到合并后的 AGENT。
            if raw_mode in (cls.AGENT_PLAN, cls.AGENT_FAST):
                return cls.AGENT
            return raw_mode
        if not isinstance(raw_mode, str):
            return fallback
        normalized = raw_mode.strip().lower()
        if not normalized:
            return fallback
        # 任何 agent* 请求（agent / agent.plan / agent.fast）归一到 AGENT。
        if normalized.split(".", 1)[0] == "agent":
            return cls.AGENT
        # 历史裸 plan / fast（同 CLI MODE_ALIASES）显式归一到 AGENT，
        # 不依赖 fallback 默认值恰好等于 AGENT。
        if normalized in ("plan", "fast"):
            return cls.AGENT
        try:
            return cls(normalized)
        except ValueError:
            return fallback

    def to_runtime_mode(self) -> str:
        """输出 runtime mode 值；历史 agent.plan / agent.fast 归一为 agent。"""
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
    mode: Mode = Mode.AGENT
    is_stream: bool = False
    stream_seq: int | None = None
    stream_id: str | None = None
    metadata: dict[str, Any] | None = None
    group_digital_avatar: bool = False
    enable_memory: bool | None = None
    enable_streaming: bool = True
