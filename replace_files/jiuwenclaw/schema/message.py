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
    HISTORY_GET = "history.get"
    COMMAND_ADD_DIR = "command.add_dir"
    COMMAND_CHROME = "command.chrome"
    COMMAND_COMPACT = "command.compact"
    COMMAND_DIFF = "command.diff"
    COMMAND_LS = "command.ls"
    COMMAND_VIEW = "command.view"
    COMMAND_MODEL = "command.model"
    COMMAND_RESUME = "command.resume"
    COMMAND_SESSION = "command.session"

    CONFIG_GET = "config.get"
    CONFIG_SET = "config.set"
    CHANNEL_GET = "channel.get"

    SESSION_LIST = "session.list"
    SESSION_CREATE = "session.create"
    SESSION_DELETE = "session.delete"
    SESSION_RENAME = "session.rename"

    PATH_GET = "path.get"
    PATH_SET = "path.set"

    BROWSER_START = "browser.start"
    BROWSER_RUNTIME_RESTART = "browser.runtime_restart"

    CONFIG_CACHE_CLEAR = "config.cache_clear"
    AGENT_RELOAD_CONFIG = "agent.reload_config"

    MEMORY_COMPUTE = "memory.compute"

    FILES_LIST = "files.list"
    FILES_GET = "files.get"
    TTS_SYNTHESIZE = "tts.synthesize"

    SKILLS_MARKETPLACE_LIST = "skills.marketplace.list"
    SKILLS_LIST = "skills.list"
    SKILLS_INSTALLED = "skills.installed"
    SKILLS_GET = "skills.get"
    SKILLS_INSTALL = "skills.install"
    SKILLS_IMPORT_LOCAL = "skills.import_local"
    SKILLS_MARKETPLACE_ADD = "skills.marketplace.add"
    SKILLS_MARKETPLACE_REMOVE = "skills.marketplace.remove"
    SKILLS_MARKETPLACE_TOGGLE = "skills.marketplace.toggle"
    SKILLS_UNINSTALL = "skills.uninstall"
    SKILLS_SKILLNET_SEARCH = "skills.skillnet.search"
    SKILLS_SKILLNET_INSTALL = "skills.skillnet.install"
    SKILLS_SKILLNET_INSTALL_STATUS = "skills.skillnet.install_status"
    SKILLS_SKILLNET_EVALUATE = "skills.skillnet.evaluate"
    SKILLS_CLAWHUB_GET_TOKEN = "skills.clawhub.get_token"
    SKILLS_CLAWHUB_SET_TOKEN = "skills.clawhub.set_token"
    SKILLS_CLAWHUB_SEARCH = "skills.clawhub.search"
    SKILLS_CLAWHUB_DOWNLOAD = "skills.clawhub.download"
    SKILLS_EVOLUTION_STATUS = "skills.evolution.status"
    SKILLS_EVOLUTION_GET = "skills.evolution.get"
    SKILLS_EVOLUTION_SAVE = "skills.evolution.save"

    EXTENSIONS_LIST = "extensions.list"
    EXTENSIONS_IMPORT = "extensions.import"
    EXTENSIONS_DELETE = "extensions.delete"
    EXTENSIONS_TOGGLE = "extensions.toggle"

    HEARTBEAT_GET_CONF = "heartbeat.get_conf"
    HEARTBEAT_SET_CONF = "heartbeat.set_conf"

    # 安全防护 permissions（与 Web ``register_method`` 同名，经 E2A → AgentServer 处理；owner_scopes 仅走 Web 直连）
    PERMISSIONS_TOOLS_GET = "permissions.tools.get"
    PERMISSIONS_ENABLED_GET = "permissions.enabled.get"
    PERMISSIONS_ENABLED_SET = "permissions.enabled.set"
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

    CHANNEL_FEISHU_GET_CONF = "channel.feishu.get_conf"
    CHANNEL_FEISHU_SET_CONF = "channel.feishu.set_conf"

    CHANNEL_XIAOYI_GET_CONF = "channel.xiaoyi.get_conf"
    CHANNEL_XIAOYI_SET_CONF = "channel.xiaoyi.set_conf"

    CHANNEL_TELEGRAM_GET_CONF = "channel.telegram.get_conf"
    CHANNEL_TELEGRAM_SET_CONF = "channel.telegram.set_conf"
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

# SkillDev 模式请求方法
    SKILLDEV_CHAT = "skilldev.chat"  # 发起用户消息
    SKILLDEV_START = "skilldev.start"  # 发起用户消息
    SKILLDEV_RESPOND = "skilldev.respond"  # 统一确认入口（后端根据 task_id 当前阶段自动路由）
    SKILLDEV_USER_ANSWER = "skilldev.user_answer"  # 结构化提问（ask_user_question）用户选择
    SKILLDEV_STATUS = "skilldev.status"  # 查询状态（不传 task_id → 返回任务列表）
    SKILLDEV_SESSION_LIST = "skilldev.session.list"  # 查询 SkillDev 会话列表
    SKILLDEV_RESTORE = "skilldev.restore"  # 恢复 SkillDev 会话
    SKILLDEV_PARSE_SKILL = "skilldev.parse_skill"  # 导入本地 skill 压缩包到任务工作区
    SKILLDEV_DOWNLOAD = "skilldev.download"  # 下载产物
    SKILLDEV_CANCEL = "skilldev.cancel"  # 取消任务
    SKILLDEV_FILE_LIST = "skilldev.file.list"  # 获取工作区文件树（产物弹窗浏览）
    SKILLDEV_FILE_READ = "skilldev.file.read"  # 读取工作区文件内容
    SKILLDEV_FILE_WRITE = "skilldev.file.write"  # 覆盖写入工作区文本文件
    SKILLDEV_RESOURCE_DELETE = "skilldev.resource.delete"  # 按身份删除 message.send 上传的资源
    SKILLDEV_BATCH_UPLOAD = "skilldev.batch_upload"  # 批量打包上传 workspace 到 OBS
    SKILLDEV_BATCH_DOWNLOAD = "skilldev.batch_download"  # 批量下载解压 workspace
    SKILLDEV_ANSWER = "skilldev.user_answer"

    TOOLS_ADD = "tools.add"

    # 文件传输方法（分布式部署）
    FILE_TRANSFER_START = "file.transfer.start"
    FILE_TRANSFER_CHUNK = "file.transfer.chunk"
    FILE_TRANSFER_COMPLETE = "file.transfer.complete"

class EventType(Enum):
    CONNECTION_ACK = "connection.ack"
    HELLO = "hello"
    CHAT_DELTA = "chat.delta"
    CHAT_REASONING = "chat.reasoning"
    CHAT_USAGE_METADATA = "chat.usage_metadata"
    CHAT_USAGE_SUMMARY = "chat.usage_summary"
    CHAT_FINAL = "chat.final"
    CHAT_MEDIA = "chat.media"
    CHAT_FILE = "chat.file"
    CHAT_TOOL_CALL = "chat.tool_call"
    CHAT_TOOL_CALLS_DELTA = "chat.tool_calls.delta"
    CHAT_TOOL_UPDATE = "chat.tool_update"
    CHAT_TOOL_RESULT = "chat.tool_result"
    CONTEXT_COMPRESSED = "context.compressed"
    CONTEXT_USAGE = "context.usage"
    TODO_UPDATED = "todo.updated"
    CHAT_PROCESSING_STATUS = "chat.processing_status"
    CHAT_ERROR = "chat.error"
    CHAT_INTERRUPT_RESULT = "chat.interrupt_result"
    CHAT_EVOLUTION_STATUS = "chat.evolution_status"
    CHAT_SUBTASK_UPDATE = "chat.subtask_update"
    TASK_START = "task.start"
    TASK_COMPLETE = "task.complete"
    CHAT_ASK_USER_QUESTION = "chat.ask_user_question"
    CHAT_SESSION_RESULT = "chat.session_result"
    TEAM_MEMBER = "team.member"
    TEAM_TASK = "team.task"
    TEAM_MESSAGE = "team.message"
    HEARTBEAT_RELAY = "heartbeat.relay"
    HISTORY_GET = "history.message"
    # SkillDev 事件类型
    SKILLDEV_STARTED = "skilldev.started"
    SKILLDEV_STAGE_CHANGED = "skilldev.stage_changed"
    SKILLDEV_PROGRESS = "skilldev.progress"
    SKILLDEV_AGENT_THINKING = "skilldev.agent_thinking"
    SKILLDEV_AGENT_OUTPUT = "skilldev.agent_output"
    SKILLDEV_TEST_PROGRESS = "skilldev.test_progress"
    SKILLDEV_TODOS_UPDATE = "skilldev.todos_update"
    SKILLDEV_CONFIRM_REQUEST = "skilldev.confirm_request"
    SKILLDEV_CONFIRM_RESOLVED = "skilldev.confirm_resolved"
    SKILLDEV_ARTIFACT_READY = "skilldev.artifact_ready"
    SKILLDEV_EVAL_READY = "skilldev.eval_ready"
    SKILLDEV_SKILL_NAME_READY = "skilldev.skill_name_ready"
    SKILLDEV_VALIDATE_RESULT = "skilldev.validate_result"
    SKILLDEV_DESC_OPT_READY = "skilldev.desc_opt_ready"
    SKILLDEV_ERROR = "skilldev.error"
    SKILLDEV_SUSPENDED = "skilldev.suspended"
    SKILLDEV_COMPLETED = "skilldev.completed"
    SKILLDEV_TOOL_CALL = "skilldev.tool_call"
    SKILLDEV_TOOL_RESULT = "skilldev.tool_result"
    SKILLDEV_FILE_READY = "skilldev.file_ready"
    SKILLDEV_GATE_OBS_CLEANUP = "skilldev.gate_obs_cleanup"


class Mode(Enum):
    AGENT_PLAN = "agent.plan"
    AGENT_FAST = "agent.fast"
    CODE_PLAN = "code.plan"
    CODE_NORMAL = "code.normal"
    TEAM = "team"
    SKILLDEV = "skilldev"

    @classmethod
    def from_raw(cls, raw_mode: Any, default: "Mode | None" = None) -> "Mode":
        """解析 mode，仅接受新值(agent.plan/agent.fast/code.plan/code.normal/team)。"""
        fallback = default or cls.AGENT_PLAN
        if isinstance(raw_mode, Mode):
            return raw_mode
        if not isinstance(raw_mode, str):
            return fallback
        normalized = raw_mode.strip().lower()
        if not normalized:
            return fallback
        try:
            return cls(normalized)
        except ValueError:
            return fallback

    def to_runtime_mode(self) -> str:
        """输出新 mode 值本身。"""
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
    bot_id: str | None = None
    payload: dict | None = None
    req_method: ReqMethod | None = None
    event_type: EventType | None = None
    mode: Mode = Mode.AGENT_PLAN
    is_stream: bool = False
    stream_seq: int | None = None
    stream_id: str | None = None
    metadata: dict[str, Any] | None = None
    group_digital_avatar: bool = False
    enable_memory: bool | None = None
    enable_streaming: bool = True
