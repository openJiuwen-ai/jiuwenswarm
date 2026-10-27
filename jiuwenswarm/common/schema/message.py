# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""统一消息模型."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from openjiuwen_protocol.message import ReqMethod

from gateway.common.mode_matrix import deprecate_mode

# Wire names shared by gateway and AgentServer. Gateway matches these strings
# and does not import the agents or server modules that produce them.
SESSION_REQUEST_COMPLETED_EVENT = "chat.request_completed"
PROMPT_SUGGESTION_READY_EVENT = "prompt_suggestion.ready"
TOPIC_STARTERS_READY_EVENT = "topic_starters.ready"
TOPIC_STARTERS_CONTROL_CHANGED_EVENT = "topic_starters.control_changed"


def _ensure_req_method(name: str, value: str) -> None:
    """Register a ReqMethod member the pinned protocol package does not ship yet.

    ``openjiuwen-protocol`` owns the enum. These members are the recommendation
    RPCs this process already speaks. Once the pinned revision defines the same
    name and value, this registration leaves that member in place.
    """
    existing = ReqMethod.__members__.get(name)
    if existing is not None:
        if existing.value != value:
            raise RuntimeError(
                f"ReqMethod.{name} is {existing.value!r}, expected {value!r}"
            )
        return
    if any(item.value == value for item in ReqMethod):
        raise RuntimeError(f"ReqMethod value {value!r} is already registered")
    # Enum exposes no public API for adding a member, so extending the pinned
    # protocol enum requires its internal registries. Keep those accesses here.
    member = object.__new__(ReqMethod)
    member._name_ = name  # pylint: disable=protected-access
    member._value_ = value  # pylint: disable=protected-access
    type.__setattr__(ReqMethod, name, member)
    ReqMethod._member_map_[name] = member  # pylint: disable=protected-access
    ReqMethod._value2member_map_[value] = member  # pylint: disable=protected-access
    ReqMethod._member_names_.append(name)  # pylint: disable=protected-access


_ensure_req_method("PROMPT_SUGGESTION_CANCEL", "prompt_suggestion.cancel")
_ensure_req_method("RECOMMENDATION", "recommendation.job")
# Dispatch compares this member on every request. The pinned protocol package
# does not define it, so a missing attribute fails chat and session creation
# before their own handlers run.
_ensure_req_method("MCP_OAUTH_START", "mcp.oauth.start")

logger = logging.getLogger(__name__)


# ``chat.send`` 请求携带该 truthy 参数时，仅为已在运行的 Team Session 附加
# 响应订阅，不提交新输入（gateway / runtime / server 三方共用的协议参数名）。
TEAM_ATTACH_ONLY_PARAM = "attach_only"


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
