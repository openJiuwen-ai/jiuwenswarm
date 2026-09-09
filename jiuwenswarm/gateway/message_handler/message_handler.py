# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""MessageHandler - 消息处理抽象与双队列实现（入队经 AgentServerClient 发往 AgentServer）."""

from __future__ import annotations

import logging
import asyncio
import os
import re
import secrets
import time
from abc import ABC
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, Literal
from jiuwenswarm.gateway.channel_manager.base import ChannelType
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
    E2A_WIRE_INTERNAL_METADATA_KEYS,
)
from jiuwenswarm.common.config import get_evolution_auto_save_enabled
from jiuwenswarm.gateway.routing.session_map import SessionMap
from jiuwenswarm.gateway.routing.agent_request_timeout import (
    send_agent_request_with_timeout,
)
from jiuwenswarm.gateway.message_handler.join_exit_handlers import JoinExitHandlers
from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
    ParsedControlAction,
    parse_channel_control_text,
)
from jiuwenswarm.gateway.message_handler.evolution_approval import (
    EvolutionApprovalCoordinator,
    ensure_regular_evolution_approval_metadata,
    is_evolution_approval_payload,
    is_evolution_approval_request_id,
    is_interrupt_evolution_approval_answer_payload,
)
from jiuwenswarm.gateway.message_handler.prompts.review_prompt import build_review_prompt
from jiuwenswarm.gateway.message_handler.prompts.security_review_prompt import (
    GitPreExecError,
    build_security_review_prompt,
)
from jiuwenswarm.extensions.hook_event import GatewayHookEvents
from jiuwenswarm.extensions.hooks_context import GatewayChatHookContext
from jiuwenswarm.common.hooks_config import load_hooks_config
from jiuwenswarm.common.mode_matrix import (
    DEPRECATION_MAP,
    MODE_ALIASES,
    NEW_AGENT_CODE_NORMAL,
    NEW_AGENT_CODE_PLAN,
    NEW_AGENT_WORK_NORMAL,
    NEW_AGENT_WORK_PLAN,
    NEW_CANONICAL_MODES,
    NEW_TEAM_CODE_NORMAL,
    deprecate_mode,
)
from jiuwenswarm.gateway.hooks.handler import GatewayHookHandler
from jiuwenswarm.gateway.routing.keys import RoutingKey, AgentRef, make_delivery_target
from jiuwenswarm.gateway.routing.session_sharing import SessionSharingRegistry, SubRole

logger = logging.getLogger(__name__)

_ACP_CHANNEL_ID = "acp"
_ACP_ORIGINAL_SESSION_ID_KEY = "acp_original_session_id"
# \mode 切换合法输入集：新 canonical + 旧 canonical（DEPRECATION_MAP.keys()）
# + 正式别名（MODE_ALIASES.keys()，如 team.plan / team.code）。
# 单一事实源，前置校验与分发同源，避免和 ModeSubcommand/_VALID_MODE_LINES 漂移。
# team.plan / team.code 不在 DEPRECATION_MAP 里（它们经 canonicalize_mode_text
# 先归一到 team.plan.normal / code.team，再 deprecate_mode 映到新 canonical），
# 故白名单必须显式并入 MODE_ALIASES.keys()，否则会被前置校验判「非法指令」。
_VALID_MODE_INPUTS: frozenset[str] = frozenset(
    NEW_CANONICAL_MODES | set(DEPRECATION_MAP.keys()) | set(MODE_ALIASES.keys())
)
# ACP: one in-flight chat replaces any prior work on that channel.
# TUI/CLI 已移除此列表：多窗口 TUI 各自维护独立 session，互不干扰。
_SINGLE_USER_CHANNEL_IDS = frozenset({
    ChannelType.ACP.value,
})
_TUI_DISCONNECT_CANCEL_GRACE_SECONDS = 60.0
_DEFAULT_INLINE_FILE_SIZE_LIMIT = 128 * 1024
_KNOWN_JIUWENSWARM_SESSION_PREFIXES = (
    "sess_",
    "web_",
    "tui_",
    "acp_",
    "cron_",
    "feishu_",
    "wechat_",
    "xiaoyi_",
    "dingtalk_",
    "wecom_",
    "telegram_",
    "discord_",
    "slack_",
    "whatsapp_",
)
_INTERRUPT_RESUME_SOURCES = frozenset({
    "ask_user_interrupt",
    "confirm_interrupt",
    "permission_interrupt",
    "evolution_interrupt",
})
_A2UI_OPEN_TAG_MARKER = "<a2ui-json>"
# Shown when a channel with streaming disabled asks for a team round. The team
# runtime streams member events as they happen and has no non-streaming entry
# point, so the request is refused rather than silently downgraded.
_NON_STREAM_TEAM_NOTICE = (
    "集群模式需要开启流式输出才能运行（成员协作事件是流式下发的）。"
    "请在该通道配置中开启 enable_streaming，或改用单 Agent 模式。"
)
_DELIVERY_IDENTITY_METADATA_KEYS = frozenset({
    "app_id",
    "chat_type",
    "im_chat_type",
    "feishu_chat_id",
    "feishu_open_id",
    "open_id",
    "im_sender_user_id",
    "im_thread_id",
})


def apply_a2ui_text_fallback_to_gateway_payload(
    payload: dict[str, Any],
    *,
    channel_id: str,
) -> dict[str, Any]:
    """Convert A2UI blocks to text for non-Web channel payloads."""
    if not any(_A2UI_OPEN_TAG_MARKER in value for value in payload.values() if isinstance(value, str)):
        return payload

    from jiuwenswarm.server.runtime.a2ui.integration import apply_non_web_text_fallback_to_payload

    return apply_non_web_text_fallback_to_payload(payload, channel_id=channel_id)


def normalize_legacy_health_check_relay_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Normalize the pre-split probe relay before channel fan-out."""
    if payload.get("event_type") != "heartbeat.relay":
        return payload
    payload["event_type"] = "health_check.relay"
    if "health_check" not in payload and "heartbeat" in payload:
        payload["health_check"] = payload["heartbeat"]
    return payload



class ChannelMode(str, Enum):
    AGENT = "agent"
    # 历史值：plan / fast 已合并为 agent，保留以兼容旧持久化 channel state。
    AGENT_PLAN = "agent.plan"
    AGENT_FAST = "agent.fast"
    CODE_PLAN = "code.plan"
    CODE_NORMAL = "code.normal"
    CODE_TEAM = "code.team"
    TEAM = "team"
    TEAM_PLAN = "team.plan.normal"
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
    def is_team_mode(cls, mode: str) -> bool:
        """Return True if *mode* resolves to any team variant (case-insensitive)."""
        from jiuwenswarm.common.mode_matrix import is_team_mode

        return is_team_mode(mode)


def channel_mode_from_str(mode_str: str) -> ChannelMode:
    """把任意 mode 字符串归一到 :class:`ChannelMode`。

    与 ``/mode`` 分发同源：``deprecate_mode`` 先把旧 canonical（含 team.plan 等
    别名）静默映射到新 canonical，再用 ``ChannelMode`` 直接构造；不在
    :data:`NEW_CANONICAL_MODES` 的串（如未知值、未迁移值）兜底为
    :attr:`ChannelMode.AGENT`。集中此逻辑避免 ``_get_channel_default_state``、
    ``handle_mode_switch``、``_external_session_aliases`` 等多处手抄。
    """
    new_mode_str = deprecate_mode(mode_str)
    if new_mode_str in NEW_CANONICAL_MODES:
        try:
            return ChannelMode(new_mode_str)
        except ValueError:
            logger.warning(
                "channel_mode_from_str: new canonical '%s' 命中 NEW_CANONICAL_MODES "
                "但 ChannelMode 构造失败，兜底 AGENT (raw=%r)",
                new_mode_str, mode_str,
            )
            pass
    if new_mode_str != (mode_str or "").strip().lower():
        logger.debug(
            "channel_mode_from_str: '%s' -> new canonical '%s' (非 NEW_CANONICAL_MODES，兜底 AGENT)",
            mode_str, new_mode_str,
        )
    else:
        logger.debug(
            "channel_mode_from_str: '%s' 未命中 NEW_CANONICAL_MODES，兜底 AGENT",
            mode_str,
        )
    return ChannelMode.AGENT


# ``/switch`` 子指令判据：``state.mode`` 经 ``handle_mode_switch`` 落定后必为新
# canonical，旧枚举成员（AGENT / AGENT_PLAN / CODE_PLAN / CODE_TEAM …）永不命中，
# 故判据改查新 canonical 字符串集合，行为与旧 ``in (ChannelMode.X, ...)`` 等价。
# 历史等价：``agent`` / ``agent.plan`` / ``agent.fast`` → 新 ``agent.work.*``；
# ``code.plan`` / ``code.normal`` / ``code.team`` → 新 ``agent.code.plan`` /
# ``agent.code.normal`` / ``team.code.normal``。
_SWITCH_AGENT_WORK_MODES: frozenset[str] = frozenset(
    {NEW_AGENT_WORK_NORMAL, NEW_AGENT_WORK_PLAN}
)
_SWITCH_CODE_MODES: frozenset[str] = frozenset(
    {NEW_AGENT_CODE_PLAN, NEW_AGENT_CODE_NORMAL, NEW_TEAM_CODE_NORMAL}
)


@dataclass
class ChannelControlState:
    session_id: str | None = None
    mode: ChannelMode = ChannelMode.AGENT


@dataclass
class NewSessionCancelParams:
    """\\new_session 时取消旧会话并发通知所需的具名参数（避免过长形参列表）。"""

    user_infos: dict[str, Any]
    channel_id: str
    reply_session_id: str | None
    new_sid: str
    old_sid: str | None


@dataclass
class ModeChangeCancelParams:
    """\\mode 切换时取消旧会话并发通知所需的具名参数。"""

    user_infos: dict[str, Any]
    channel_id: str
    reply_session_id: str | None
    old_sid: str | None
    new_mode_label: str


if TYPE_CHECKING:
    from jiuwenswarm.common.e2a.models import E2AEnvelope
    from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
    from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
    from jiuwenswarm.common.schema.message import Message


# ---------- 双队列实现：入队经 AgentServerClient 发往 AgentServer ----------
class MessageHandler(ABC):
    """
    维护两个异步消息队列，入队消息通过 AgentServerClient 发送给 AgentServer：

    - _user_messages：Channel 发来的消息，由内部转发循环消费并调用 agent_client.send_request
    - _robot_messages：AgentServer 的响应，由 ChannelManager 消费并派发到对应 Channel

    AgentServer 经 WebSocket 下行 **E2AResponse** 线 JSON；``WebSocketAgentServerClient`` 内
    （``jiuwenswarm.e2a.wire_codec``）解析并还原为 ``AgentResponse`` / ``AgentResponseChunk``，
    本类仍通过 ``_response_to_message`` / ``_chunk_to_message`` 转为 ``Message`` 供 Channel 消费。

    单例模式：全局仅存在一个 MessageHandler 实例，可通过 MessageHandler(client) 或
    MessageHandler.get_instance(client) 获取。
    """

    _instance: "MessageHandler | None" = None

    def __new__(cls, agent_client: "AgentServerClient", *args: Any, **kwargs: Any) -> "MessageHandler":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, agent_client: "AgentServerClient") -> None:
        if getattr(self, "_singleton_initialized", False):
            return
        self._singleton_initialized = True
        self.agent_client = agent_client
        self._user_messages: asyncio.Queue["Message"] = asyncio.Queue()
        self._robot_messages: asyncio.Queue["Message"] = asyncio.Queue()
        self._running = False
        self._forward_task: asyncio.Task | None = None
        self._stream_tasks: dict[str, asyncio.Task] = {}  # request_id -> task
        self._stream_channels: dict[str, str] = {}  # request_id -> channel_id
        self._stream_sessions: dict[str, str | None] = {}  # request_id -> session_id
        self._stream_metadata: dict[str, dict[str, Any] | None] = {}  # request_id -> request metadata
        # AgentServer server_push frames only carry request_id. Keep the
        # authenticated owner so cron mutations and reverse E2A stay routed.
        self._stream_user_ids: dict[str, str] = {}
        self._stream_modes: dict[str, str] = {}  # request_id -> mode
        self._stream_emits_processing_status: dict[str, bool] = {}  # request_id -> emits chat.processing_status
        # request_id -> req_method value（如 chat.send / command.goal / history.get）。
        # 用于 processing_status=false 守卫：仅 chat.send(emit=True) 与 command.goal
        # 长流可挡住补发；history.get 等短只读流不得挡住。
        self._stream_methods: dict[str, str] = {}
        # 非流式 chat.send（如飞书 enable_streaming=False）任务追踪：rid -> mode。
        # 流式任务走 _stream_emits_processing_status；非流式 chat 不产生 processing_status，
        # 但同样是“用户发起的对话任务在跑”，配置保存锁须覆盖，故单列此集合。
        # value 存 mode（与 _stream_modes 同款），供 has_active_streams() 判断 team 排除用。
        self._active_chat_tasks: dict[str, str] = {}
        self._disconnect_cancel_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._stream_app_ids: dict[str, str] = {}  # request_id -> app_id, 多应用流式精确路由
        self._fire_and_forget_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
        self._evolution_approval = EvolutionApprovalCoordinator()
        # 配置仅在启动/成功热重载时解析；流式 chunk 热路径直接读取该内存值，
        # 避免每个审批事件重新读取磁盘配置。
        self._evolution_auto_save_enabled = False
        self._session_last_user_query: dict[str, str] = {}
        # session_id -> 最近一次人类发起请求的 (channel_id, member_name)。
        # team 模式下 file msg 不携带发起者身份（rid 固定为建会话那轮），send_file 定向
        # 投递时按 session_id 取最近发起者兜底（并发时可能取到另一 human，仅投错人不泄漏）。
        self._session_last_originator: dict[str, tuple[str, str]] = {}
        self._acp_session_aliases: dict[str, str] = {}  # external_session_id -> internal_session_id
        self._acp_session_alias_lock = asyncio.Lock()
        self._external_session_aliases: dict[tuple[str, str], str] = {}
        self._external_session_alias_lock = asyncio.Lock()

        # per-channel 控制状态：支持 \new_session / \mode 指令。
        # 使用 ChannelType 的 value 作为标准键，避免散落的硬编码字符串。
        self._control_channel_types = {
            ChannelType.FEISHU.value,
            ChannelType.XIAOYI.value,
            ChannelType.DINGTALK.value,
            ChannelType.WHATSAPP.value,
            ChannelType.WECOM.value,
            ChannelType.WECHAT.value,
        }
        # 使用 SessionMap 的 channel 族（由 config 中 gateway.session_map_scope 决定是否在 key 中含 user）
        # feishu 普通通道同样纳入：否则 _apply_channel_state 把 msg.session_id 置 None 后，
        # _forward_loop 用 None 做 key 重新 get_or_create_channel_state，sid 写进
        # "feishu:None" 而复用查 "feishu:oc_xxx"，永远对不上，每条消息都新建 session、丢上下文。
        # 走 SessionMap 用 identity_key(provider,chat_id,bot_id[,user_id]) 查找，不依赖
        # msg.session_id，且落盘 session_map.json，顺带支持跨进程重启复用。
        self._session_map_channel_types = frozenset({
            "feishu",
            "feishu_enterprise",
        })
        self._channel_states: Dict[str, ChannelControlState] = {}
        self._session_map = SessionMap()
        self._session_sharing = SessionSharingRegistry()
        # 组合：/join /exit 团队成员管理逻辑（独立文件维护，通过 self._h 访问宿主能力）
        self._join_exit = JoinExitHandlers(self)
        self._cron_controller = None

        # IM Pipeline（数字分身）— None 时不执行，不影响原有逻辑
        self._inbound_pipeline = None   # type: Any  # IMInboundPipeline | None
        self._outbound_pipeline = None  # type: Any  # IMOutboundPipeline | None

        # 初始化 Gateway hooks handler
        try:
            from jiuwenswarm.common.config import get_config
            config_base = get_config()
            self._gateway_hooks_config = load_hooks_config(config_base)
            self._gateway_hook_handler = GatewayHookHandler(self._gateway_hooks_config)
        except Exception as e:
            logger.warning("[MessageHandler] Failed to init GatewayHookHandler: %s", e)
            self._gateway_hook_handler = None

    def get_session_sharing_registry(self) -> SessionSharingRegistry:
        """返回 SessionSharingRegistry 实例，供 V2 共享会话路由使用."""
        return self._session_sharing

    async def unregister_ws_subscriptions(self, channel_id: str, ws_id: str) -> int:
        """Remove physical subscriptions owned by a disconnected WebSocket."""
        return await self._session_sharing.unregister_by_ws_id(
            ws_id,
            channel_id=channel_id,
        )

    def trigger_session_start_hook(self, session_id: str, source: str = "startup") -> None:
        """供 Channel 层调用，触发 SessionStart hook."""
        if self._gateway_hook_handler:
            asyncio.create_task(
                self._gateway_hook_handler.on_session_start(session_id, source=source)
            )

    def set_inbound_pipeline(self, pipeline: Any) -> None:
        self._inbound_pipeline = pipeline

    def set_outbound_pipeline(self, pipeline: Any) -> None:
        self._outbound_pipeline = pipeline

        # 直接使用 jiuwenswarm.config 的 get_config_raw/set_config/update_channel_in_config
        # 避免在此处重复实现 config 模块加载逻辑。
        from jiuwenswarm.common.config import get_config_raw, update_channel_in_config

        self._get_config_raw = get_config_raw
        self._update_channel_in_config = update_channel_in_config

        self._register_agent_server_push_handler()

    def _register_agent_server_push_handler(self) -> None:
        """Attach the side-channel callback for local and routed clients."""
        # Both the local WebSocket client and AgentOSRouterClient expose this
        # callback.  Restricting registration to WebSocketAgentServerClient
        # silently discarded AgentOS AgentServer ``send_push`` frames, so cron
        # tool mutations never reached the Gateway-owned store.
        setter = getattr(self.agent_client, "set_server_push_handler", None)
        if callable(setter):
            setter(self._handle_agent_server_push)

    def update_evolution_auto_save(self, config_payload: dict[str, Any] | None) -> None:
        """Refresh the in-memory evolution auto-save flag from a config snapshot.

        Callers should invoke this at startup and after a successful config hot
        reload.  The stream/chunk path intentionally does not call the config
        resolver so it never performs a disk read.
        """
        self._evolution_auto_save_enabled = get_evolution_auto_save_enabled(
            config_payload if isinstance(config_payload, dict) else {}
        )

    @classmethod
    def get_instance(cls, agent_client: "AgentServerClient | None" = None) -> "MessageHandler":
        """获取单例实例。

        - 若实例已存在：可直接调用 get_instance() 或 get_instance(None)，无需传入 client。
        - 若尚未创建：需传入 agent_client，即 get_instance(client) 或 MessageHandler(client)。
        """
        if cls._instance is not None:
            return cls._instance
        if agent_client is None:
            raise RuntimeError(
                "MessageHandler 尚未初始化，请先使用 MessageHandler(client) 或 get_instance(client) 创建"
            )
        return cls(agent_client)

    @staticmethod
    def extract_session_id_from_ref(session_ref: str | None) -> str | None:
        """从 session_ref 提取 session_id。

        支持两种格式：
        - 完整: team_<name>_session_<id> → 提取 <id>
        - 简化: <session_id> → 直接返回
        """
        if not session_ref:
            return None
        parts = session_ref.split("_session_")
        if len(parts) == 2:
            return parts[1]
        # 简化格式：直接就是 session_id（如 sess_xxx）
        return session_ref

    @staticmethod
    def extract_team_name_from_ref(session_ref: str | None) -> str:
        if not session_ref:
            return "unknown"
        prefix = session_ref.replace("team_", "", 1)
        return prefix.split("_session_")[0]

    @staticmethod
    def resolve_app_id(msg: "Message") -> str:
        """从 Message 提取有效的 app_id。

        msg.app_id 是 V2 新增字段，渠道尚未迁移填充（始终为 None）；
        兜底读 msg.bot_id——渠道已将其设为 channel.config.app_id。
        修复后后续可将 msg.bot_id 逐步迁移为 msg.app_id。
        """
        return getattr(msg, "app_id", None) or getattr(msg, "bot_id", None) or "default"

    async def handle_message(self, msg: "Message") -> None:
        """Channel 同步回调：将消息放入 user_messages 队列，由转发循环发给 AgentServer."""
        # 非控制指令: 反查发送者身份
        result = self._session_sharing.resolve_member_by_user(
            msg.channel_id,
            MessageHandler.resolve_app_id(msg),
            msg.user_id or (msg.metadata or {}).get("im_sender_user_id", ""),
            chat_id=(msg.metadata or {}).get("im_thread_id", "") if isinstance(msg.metadata, dict) else "",
        )
        if result:
            sid, mname = result
            # V2: whoami 命中时始终覆盖为 team session_id，
            # 因为 IM 消息的 session_id 是 chat_id（如 oc_xxx），
            # 不加覆盖则消息发到错误 session，LLM 视为普通对话。
            msg.session_id = sid
            msg.metadata = dict(msg.metadata or {})
            msg.metadata["member_name"] = mname

        self._remember_user_query_context(msg)
        self._user_messages.put_nowait(msg)
        logger.info(
            "[MessageHandler] _user_messages 入队: id=%s channel_id=%s session_id=%s",
            msg.id, msg.channel_id, msg.session_id,
        )

    async def _maybe_register_godview(self, msg: "Message") -> None:
        """V2: auto-register a GodView subscriber for the channel.

        Called from _forward_loop after _apply_channel_state, so msg.session_id is
        already the real team session_id (not the inbound chat_id like oc_xxx) and
        msg.params.mode has been injected. Registering here ensures GodView lands
        under the same session_id that team-event dispatch uses, so fan_out's
        godview intent can resolve.

        Trigger when:
        - params.mode is a team variant (team / code.team / team.plan), or
        - session already has subscribers (already a team session; web mode may be "agent")
        """
        if not msg.session_id:
            return
        req_method = str(getattr(getattr(msg, "req_method", None), "value", "") or "")
        # External publishers share the Web/TUI transport endpoint but are not
        # observers. Registering their short-lived socket as GodView leaves a
        # dead delivery target as soon as the publish command exits.
        if req_method == "team.mq.publish":
            return
        # member 已通过 /join 认领席位（resolve_member_by_user 命中），其消息走
        # mention/private intent 精确投递即可，无需再为本 channel 注册 GodView
        # （否则 member 会多收一份 godview 全量输出，飞书端还会与 mention/private
        # 拼到同一 buffer 串台）。单人 /mode team 无 member_name，正常注册 GodView。
        if isinstance(msg.metadata, dict) and msg.metadata.get("member_name"):
            return
        _params = msg.params if isinstance(msg.params, dict) else {}
        _mode = str(_params.get("mode") or "")
        _session_has_subs = bool(self._session_sharing.lookup_all(msg.session_id))
        if not (ChannelMode.is_team_mode(_mode) or _session_has_subs):
            return
        godview_subs = self._session_sharing.lookup_member(msg.session_id, SubRole.GODVIEW)
        _ch = msg.channel_id or "web"
        _kind = "ws" if _ch in ("web", "tui") else "group"
        _ws_id = (msg.metadata or {}).get("ws_id", "")
        _has_godview_for_this_channel = any(
            s.routing_key.channel_id == _ch
            and (
                _kind != "ws"
                or getattr(s.delivery, "ws_id", "") == _ws_id
            )
            for s in godview_subs
        )
        if _has_godview_for_this_channel:
            logger.info(
                "[MessageHandler] GodView already registered for channel %s: session=%s count=%d",
                _ch, msg.session_id, len(godview_subs),
            )
            return
        _app = MessageHandler.resolve_app_id(msg)
        # ws_id is generated by Channel handshake and injected into msg.metadata;
        # send resolves by delivery.ws_id first. IM channel has no ws_id, uses chat_id
        # as container.
        _user = (
            (msg.metadata or {}).get("user_id")
            or msg.user_id
            or str(getattr(msg, "chat_id", None) or _ch)
        )
        # V2: tui 同 web 走 ws 物理寻址——TuiChannel 持有 _ws_by_id，GodView 订阅
        # 需带真 ws_id 才能让 team 出站 dispatch_to_session → TuiChannel.send 命中 ws。
        # GatewayServer forward 分支已把 ws_id 注入 msg.metadata（委托 TuiChannel._register）。
        # 阶段2: GodView RoutingKey.agent_ref 与入站 _register 的 rk.agent_ref 同源
        # （用 msg.agent_ref，tui 已在 GatewayServer forward 分支按 mode/agent_id 合成），
        # 使出站 routing_keys 兜底能命中 _clients_by_key。msg.agent_ref 缺失/非 AgentRef
        # 时回退 AgentRef("team","default")（IM 等非 ws_channel 路径原语义）。
        _godview_agent_ref = getattr(msg, "agent_ref", None)
        if not isinstance(_godview_agent_ref, AgentRef):
            _godview_agent_ref = AgentRef("team", "default")
        rk = RoutingKey(
            user_id=_user,
            channel_id=_ch,
            app_id=_app,
            agent_ref=_godview_agent_ref,
            session_id=msg.session_id,
        )
        dt = make_delivery_target(
            _ch,
            chat_id=getattr(msg, "chat_id", None) or "",
            ws_id=_ws_id if _kind == "ws" else "",
            thread_ts=(msg.metadata or {}).get("slack_thread_ts", ""),
            chat_type=(msg.metadata or {}).get("slack_channel_type", "group"),
        )
        await self._session_sharing.register(msg.session_id, SubRole.GODVIEW, rk, dt)
        logger.info(
            "[MessageHandler] GodView auto-registered: session=%s channel=%s user=%s app=%s kind=%s"
            " rk_full=%s",
            msg.session_id, _ch, _user, _app, _kind,
            {"user_id": rk.user_id, "channel_id": rk.channel_id, "app_id": rk.app_id,
             "agent_ref": str(rk.agent_ref), "session_id": rk.session_id},
        )

    # ---------- Channel 控制状态：\new_session / \mode ----------

    def _remember_user_query_context(self, msg: "Message") -> None:
        if not self._is_chat_send_message(msg):
            return
        if not isinstance(msg.params, dict) or msg.params.get("is_supplement") is True:
            return
        session_id = str(msg.session_id or "").strip()
        if not session_id:
            return
        query = str(msg.params.get("query") or msg.params.get("content") or "").strip()
        if not query:
            return
        self._session_last_user_query[session_id] = query[:8000]
        # 记录最近人类发起者身份（member_name 由 resolve_member_by_user 注入 msg.metadata），
        # 供 send_file 在 file msg 不携带发起者时按 session_id 反查定向。
        # 排除 GodView：它是通道级自动注册的伪 member，非真实 /join 人类席位，不应作为发起者定向。
        # 无 member_name 的入站（如 web：web 不 /join，resolve_member_by_user 不命中）→ 清空
        # last-originator，避免 web 发起时残留上一次 feishu 用户导致文件误投到 feishu。
        origin_member = str((msg.metadata or {}).get("member_name") or "").strip()
        if origin_member and origin_member != SubRole.GODVIEW:
            self._session_last_originator[session_id] = (
                str(msg.channel_id or "").strip(),
                origin_member,
            )
        else:
            self._session_last_originator.pop(session_id, None)

    def _get_session_last_user_query(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        return self._session_last_user_query.get(str(session_id), "")

    def get_session_last_originator(self, session_id: str | None) -> tuple[str, str] | None:
        """返回该 session 最近一次人类发起请求的 (channel_id, member_name)，供 send_file 定向。"""
        if not session_id:
            return None
        return self._session_last_originator.get(str(session_id))

    def _attach_original_request_to_ask_user_answer(self, msg: "Message") -> "Message":
        if not isinstance(msg.params, dict):
            return msg
        if str(msg.params.get("source") or "").strip() != "ask_user_interrupt":
            return msg
        if msg.params.get("original_request"):
            return msg
        original_request = self._get_session_last_user_query(msg.session_id)
        if not original_request:
            return msg

        params = dict(msg.params)
        params["original_request"] = original_request
        return replace(msg, params=params)

    @staticmethod
    def _is_chat_send_message(msg: "Message") -> bool:
        method = getattr(msg, "req_method", None)
        value = getattr(method, "value", method)
        return value == "chat.send" or str(value) == "ReqMethod.CHAT_SEND"

    @staticmethod
    def _is_team_chat_send(msg: "Message") -> bool:
        if not isinstance(msg.params, dict):
            return False
        return ChannelMode.is_team_mode(str(msg.params.get("mode") or ""))

    @classmethod
    def _is_unsupported_non_stream_team_send(cls, msg: "Message") -> bool:
        """Whether this is a team ``chat.send`` on a non-streaming channel.

        Team rounds only exist on the streaming path: ``process_message_impl``
        has no team branch, so a non-streaming request would silently run as a
        single agent. Rejecting at the gateway keeps that surprise out of the
        conversation instead of answering as the wrong runtime.

        Args:
            msg: The inbound message, after ``_apply_channel_state`` injected mode.

        Returns:
            True when the request must be refused.
        """
        if getattr(msg, "enable_streaming", True):
            return False
        return cls._is_chat_send_message(msg) and cls._is_team_chat_send(msg)

    async def _reject_non_stream_team_send(self, msg: "Message") -> None:
        """Answer a non-streaming team request with an explanation."""
        from jiuwenswarm.common.schema.message import EventType, Message

        metadata = msg.metadata if isinstance(msg.metadata, dict) else None
        out = Message(
            id=msg.id,
            type="event",
            channel_id=msg.channel_id,
            session_id=msg.session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": EventType.CHAT_FINAL.value,
                "content": _NON_STREAM_TEAM_NOTICE,
                "is_complete": True,
            },
            event_type=EventType.CHAT_FINAL,
            metadata=metadata,
            enable_streaming=False,
        )
        await self.publish_robot_messages(out)
        logger.warning(
            "[MessageHandler] rejected non-streaming team chat.send: "
            "channel_id=%s session_id=%s mode=%s",
            msg.channel_id,
            msg.session_id,
            (msg.params or {}).get("mode") if isinstance(msg.params, dict) else None,
        )

    @classmethod
    def _is_interrupt_resume_chat_send(cls, msg: "Message") -> bool:
        if not isinstance(msg.params, dict):
            return False
        source = str(msg.params.get("source") or "").strip()
        answers = msg.params.get("answers")
        request_id = str(msg.params.get("request_id") or "").strip()
        if bool(request_id) and isinstance(answers, list) and source in _INTERRUPT_RESUME_SOURCES:
            return True
        return cls._is_interrupt_evolution_approval_answer_payload(msg.params)

    @classmethod
    def _should_cancel_existing_stream_before_chat_send(cls, msg: "Message") -> bool:
        # 主动推荐消息不取消现有流式任务，避免干扰用户当前对话
        if isinstance(msg.params, dict) and msg.params.get("source") == "proactive_recommendation":
            return False
        return (
            cls._is_chat_send_message(msg)
            and not cls._is_team_chat_send(msg)
            and not cls._is_interrupt_resume_chat_send(msg)
            and not cls._is_interaction_managed_chat_send(msg)
        )

    @staticmethod
    def _is_interaction_managed_chat_send(msg: "Message") -> bool:
        """Whether this input must reach DeepAgent without host stream replacement.

        Ordinary chat keeps JiuwenSwarm's established replace flow: Gateway
        finishes the previous host stream before starting the next request.
        Clients that explicitly set ``input_mode`` / ``runtime_mode`` to
        ``steer`` or ``follow_up``, or set ``attach_goal=true`` for the second
        step of goal set/resume, must not cancel the existing host stream —
        OpenJiuwen routes those inputs atomically while the output consumer
        remains attached.

        Requests without those fields still cancel-then-start (pre-Goal
        behavior).
        """
        params = msg.params if isinstance(msg.params, dict) else {}
        if params.get("attach_goal") is True:
            return True
        input_mode = str(
            params.get("input_mode") or params.get("runtime_mode") or ""
        ).strip().lower()
        return input_mode in {"follow_up", "steer"}

    def _get_channel_default_state(self, channel_id: str) -> ChannelControlState:
        """从 config.yaml 读取 Channel 的默认 session_id / mode."""
        try:
            cfg: Dict[str, Any] = self._get_config_raw()
        except Exception:  # noqa: BLE001
            cfg = {}
        channels_cfg = cfg.get("channels") or {}
        ch_cfg = channels_cfg.get(channel_id) or {}
        sid_raw = ch_cfg.get("default_session_id") or ""
        sid = str(sid_raw).strip() or None
        mode_raw = str(ch_cfg.get("default_mode") or "agent").strip().lower()
        # 与 /mode 分发同源：deprecate_mode 把旧 canonical（含 team.plan 等别名）
        # 静默映射到新 canonical，再以 ChannelMode 直接构造（P2 已加 8 个新成员）。
        # 历史值 plan / fast 经 deprecate_mode 归一到 agent.work.normal；agent.plan
        # 是真实 plan 模式，经 deprecate_mode 落到 agent.work.plan；裸 code 归一到
        # agent.code.normal；不在 NEW_CANONICAL_MODES 的串（如未来未迁移值）兜底为
        # AGENT，与旧 mode_map.get(mode_raw, ChannelMode.AGENT) 一致。
        mode = channel_mode_from_str(mode_raw)
        return ChannelControlState(session_id=sid, mode=mode)

    def _get_channel_state_key(self, channel_id: str, conversation_id: str | None) -> str:
        """生成 channel 状态的复合键：channel_id:conversation_id."""
        if conversation_id:
            return f"{channel_id}:{conversation_id}"
        return channel_id

    def get_or_create_channel_state(self, msg: "Message") -> ChannelControlState:
        """获取或创建消息对应 channel 状态（使用复合键）。

        conversation_id 从 msg.metadata 获取，如 feishu 的 feishu_chat_id。
        """
        ch = msg.channel_id
        # 获取 conversation_id：从不同平台的 metadata 中提取会话标识
        # feishu: feishu_chat_id, xiaoyi: xiaoyi_session_id, 其他用 session_id
        key = self._get_channel_state_key(ch, msg.session_id)

        # 如果状态已存在，直接返回
        state = self._channel_states.get(key)
        if state is not None:
            return state

        # 否则从 config 加载默认值，并缓存
        state = self._get_channel_default_state(ch)
        identity_key = self._extract_identity_tuple(msg)
        if identity_key and self._channel_id_matches_session_map_types(str(ch or "")):
            state.session_id = self._session_map.find_session_id(*identity_key)
        self._channel_states[key] = state
        return state

    def _save_channel_state_to_config(self, channel_id: str) -> None:
        """将指定 Channel 的默认 session_id / mode 写回 config.yaml."""
        state = self._channel_states.get(channel_id)
        if not state:
            return
        self._update_channel_in_config(
            channel_id,
            {
                "default_session_id": state.session_id or "",
                "default_mode": state.mode.value if hasattr(state.mode, 'value') else str(state.mode),
            },
        )

    async def _allocate_channel_session(
        self,
        msg: "Message",
        state: ChannelControlState,
        *,
        persist_session: bool = False,
    ) -> str:
        """Allocate and persist a real AgentServer-owned session for a channel."""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        channel_type = self._resolve_control_channel_type(msg)
        mode = state.mode.value
        params = dict(msg.params or {})
        create_params = {
            "create_token": secrets.token_hex(16),
            "mode": mode,
            "is_swarm": ChannelMode.is_team_mode(mode),
        }
        if persist_session:
            create_params["persist_session"] = True
        for name in ("project_id", "project_dir", "work_mode", "model_name"):
            if params.get(name) is not None:
                create_params[name] = params[name]
        env = e2a_from_agent_fields(
            request_id=f"session-create-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
            channel_id=channel_type,
            req_method=ReqMethod.SESSION_CREATE,
            params=create_params,
            is_stream=False,
            timestamp=time.time(),
        )
        resp = await self._send_non_stream_agent_request(env)
        payload = dict(resp.payload or {}) if isinstance(resp.payload, dict) else {}
        if not resp.ok:
            raise RuntimeError(str(payload.get("error") or "session.create failed"))
        sid = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
        if not sid:
            raise RuntimeError("session.create returned empty session_id")
        state.session_id = sid
        identity_key = self._extract_identity_tuple(msg)
        if identity_key and self._channel_id_matches_session_map_types(str(msg.channel_id or "")):
            self._session_map.set_session_id(*identity_key, sid)
        return sid

    async def _resolve_external_channel_session(self, msg: "Message") -> None:
        """Map A2A protocol IDs onto AgentServer-owned product Sessions.

        SSH is AgentOS relay-only and must not allocate a jiuwenswarm Session.
        """
        channel_id = str(msg.channel_id or "").strip()
        external_id = str(msg.session_id or "").strip()
        if channel_id != "a2a" or not external_id:
            return
        key = (channel_id, external_id)
        resolved = self._external_session_aliases.get(key)
        if resolved is None:
            async with self._external_session_alias_lock:
                resolved = self._external_session_aliases.get(key)
                if resolved is None:
                    raw_mode = str((msg.params or {}).get("mode") or "agent")
                    # 与 /mode 分发同源：旧 canonical 先经 deprecate_mode 映射到
                    # 新 canonical 再构造 ChannelMode，否则 a2a 通道的旧值（如
                    # agent.fast / team.plan）会被原样解析成 ValueError 兜底为
                    # AGENT，丢失原有语义。
                    mode = channel_mode_from_str(raw_mode)
                    resolved = await self._allocate_channel_session(
                        msg, ChannelControlState(mode=mode)
                    )
                    self._external_session_aliases[key] = resolved
        metadata = dict(msg.metadata or {})
        metadata.setdefault("external_session_id", external_id)
        msg.metadata = metadata
        msg.session_id = resolved

    @staticmethod
    def _extract_identity_tuple(msg: "Message") -> tuple[str, str, str, str] | None:
        provider = str(getattr(msg, "provider", None) or "").strip()
        chat_id = str(getattr(msg, "chat_id", None) or "").strip()
        bot_id = str(getattr(msg, "bot_id", None) or "").strip()
        user_id = str(getattr(msg, "user_id", None) or "").strip()
        identity_parts = (provider, chat_id, bot_id, user_id)
        if all(identity_parts):
            return (provider, chat_id, bot_id, user_id)
        return None

    def _channel_id_matches_session_map_types(self, channel_id: str) -> bool:
        """channel_id 是否属于 _session_map_channel_types 中某一族（精确匹配或 base: 前缀）."""
        cid = str(channel_id or "").strip()
        for base in self._session_map_channel_types:
            if cid == base or cid.startswith(f"{base}:"):
                return True
        return False

    def _resolve_control_channel_type(self, msg: "Message") -> str:
        """Resolve control channel type key: prefer provider, fallback to channel_id."""
        provider_raw = getattr(msg, "provider", None)
        provider = str(getattr(provider_raw, "value", provider_raw) or "").strip()
        if provider:
            return provider
        return str(getattr(msg, "channel_id", "") or "")

    async def send_channel_notice(
        self,
        user_infos: dict,
        channel_id: str,
        session_id: str | None,
        text_or_payload: str | dict[str, Any],
    ) -> None:
        """向指定 channel 发送一条系统提示消息.

        - str: 兼容历史行为，封装为 {"content": text, "is_complete": True}
        - dict: 透传给 channel（仅确保 is_complete=True）

        小艺 channel 按 payload.is_complete 判定任务结束（True → WS final=true，
        关闭「处理中」接收周期）。CLI 控制指令等 notice 均为终态回包，必须默认 True；
        若确需多帧未完结，调用方显式传入 is_complete=False。
        """
        from jiuwenswarm.common.schema.message import Message, EventType

        if isinstance(text_or_payload, dict):
            payload = dict(text_or_payload)
            payload.setdefault("is_complete", True)
        else:
            payload = {"content": text_or_payload, "is_complete": True}

        _app_id = user_infos.get("app_id") or user_infos.get("bot_id", "")
        msg = Message(
            id=user_infos['id'],
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload=payload,
            event_type=EventType.CHAT_FINAL,
            metadata=user_infos['meta_data'],
            app_id=_app_id or None,
        )
        await self.publish_robot_messages(msg)

    def _pop_stream_tracking(self, rid: str) -> None:
        """Remove all per-request stream tracking entries for *rid*."""
        self._stream_tasks.pop(rid, None)
        self._stream_channels.pop(rid, None)
        self._stream_sessions.pop(rid, None)
        self._stream_metadata.pop(rid, None)
        self._stream_user_ids.pop(rid, None)
        self._stream_emits_processing_status.pop(rid, None)
        self._stream_methods.pop(rid, None)
        self._stream_app_ids.pop(rid, None)
        self._stream_modes.pop(rid, None)

    async def _pop_stream_tracking_and_broadcast(self, rids: list[str]) -> None:
        """Pop per-request stream tracking for *rids* then refresh the global
        running snapshot to all web windows.

        Centralizes the pop+broadcast sequence so every cancel/interrupt exit
        path stays in sync. The broadcast is best-effort and never breaks the
        surrounding cancel flow.
        """
        for rid in rids:
            self._pop_stream_tracking(rid)
        if not rids:
            return
        try:
            await self._broadcast_task_global_running()
        except Exception:
            logger.debug(
                "[task.global_running] broadcast after cancel failed",
                exc_info=True,
            )

    @staticmethod
    def _is_single_user_channel(channel_id: str) -> bool:
        """Channels where a new chat.send replaces all in-flight work on that channel (ACP only)."""
        return channel_id in _SINGLE_USER_CHANNEL_IDS

    def _clone_message_for_session_cancel(
        self,
        msg: "Message",
        session_id: str,
        *,
        mode: str | None = None,
    ) -> "Message":
        """Build a Message suitable for ``_cancel_agent_work_for_session``."""
        params = dict(msg.params) if isinstance(msg.params, dict) else {}
        if mode:
            params["mode"] = mode
        return replace(msg, session_id=session_id, params=params)

    def _should_cancel_stream_on_channel(
        self,
        channel_id: str,
        new_session_id: str | None,
        task_session: str | None,
    ) -> bool:
        """Whether an ordinary chat.send replaces this in-flight host stream.

        Same-session ordinary chat preserves the pre-Goal JiuwenSwarm
        lifecycle: finish/cancel the old request before the new request starts.
        Runtime-managed Goal input is filtered by
        ``_should_cancel_existing_stream_before_chat_send`` before this method
        is reached. ACP additionally replaces an orphan stream from another
        session because it is a single-user channel.
        """
        if new_session_id and task_session == new_session_id:
            return True
        return self._is_single_user_channel(channel_id)

    def _resolve_stream_cancel_session_id(
        self,
        channel_id: str,
    ) -> str | None:
        """Best-effort session id when a stream task lacks ``_stream_sessions``."""
        for rid, sess in self._stream_sessions.items():
            if self._stream_channels.get(rid) != channel_id:
                continue
            candidate = (sess or "").strip()
            if candidate:
                return candidate
        state_key_prefix = f"{channel_id}:"
        for key in self._channel_states:
            if key == channel_id:
                continue
            if key.startswith(state_key_prefix):
                suffix = key[len(state_key_prefix):].strip()
                if suffix:
                    return suffix
        return None

    async def _cancel_stream_tasks_for_channel(
        self,
        msg: "Message",
        *,
        reason: str = "new_chat_send",
    ) -> int:
        """Cancel in-flight stream work on *msg.channel_id* before starting a new chat.send.

        Stops both gateway stream consumers and AgentServer work (via interrupt).
        Ordinary same-session chat retains the established cancel-then-start
        ordering. Explicit runtime-managed Goal input bypasses this function;
        ACP additionally drops orphan work from another session because it is
        a single-user channel.
        """
        channel_id = msg.channel_id
        new_session_id = msg.session_id
        if not channel_id:
            return 0

        candidates: list[tuple[str, asyncio.Task, str | None, str | None]] = []
        for rid, task in list(self._stream_tasks.items()):
            if self._stream_channels.get(rid) != channel_id:
                continue
            if task.done():
                continue
            task_session = self._stream_sessions.get(rid)
            if not self._should_cancel_stream_on_channel(
                channel_id, new_session_id, task_session,
            ):
                continue
            candidates.append(
                (rid, task, task_session, self._stream_modes.get(rid)),
            )

        if not candidates:
            return 0

        rids_cancelled = [rid for rid, _, _, _ in candidates]
        logger.info(
            "[MessageHandler] 取消 channel 已有流式任务 (reason=%s): channel_id=%s "
            "cancelled_rids=%s 当前并发=%d",
            reason,
            channel_id,
            rids_cancelled,
            len(self._stream_tasks),
        )

        sid_mode: dict[str, str | None] = {}
        unresolved_rids: list[str] = []
        for rid, _task, task_session, task_mode in candidates:
            sid = (task_session or "").strip()
            if not sid:
                sid = (self._resolve_stream_cancel_session_id(channel_id) or "").strip()
            if sid:
                if sid not in sid_mode:
                    sid_mode[sid] = task_mode
            else:
                unresolved_rids.append(rid)

        # Stop gateway stream consumers first — never block on AgentServer RPC.
        tasks_to_stop = [task for _rid, task, _sess, _mode in candidates]
        for task in tasks_to_stop:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks_to_stop, return_exceptions=True)
        # 走集中化 pop+broadcast：新消息顶替旧流式任务后，须让跨窗口配置保存锁
        # 感知运行态变化（否则前端保存锁卡在最后一次广播值，仅靠重连自愈）。
        await self._pop_stream_tracking_and_broadcast(
            [rid for rid, _, _, _ in candidates],
        )

        for old_sid, mode in sid_mode.items():
            cancel_msg = self._clone_message_for_session_cancel(msg, old_sid, mode=mode)
            await self._cancel_agent_work_for_session(
                cancel_msg,
                old_sid,
                publish_interrupt_result=False,
                channel_id=channel_id,
                cancel_gateway_tasks=False,
                agent_notify="await",
            )

        if unresolved_rids:
            logger.warning(
                "[MessageHandler] 流式任务取消时无法解析 session_id，"
                "已停止网关 stream 但未通知 AgentServer: channel_id=%s rids=%s",
                channel_id,
                unresolved_rids,
            )

        return len(candidates)

    async def _cancel_agent_work_for_session(
        self,
        msg: "Message",
        old_sid: str | None,
        *,
        publish_interrupt_result: bool = True,
        channel_id: str | None = None,
        cancel_gateway_tasks: bool = True,
        agent_notify: Literal["await", "fire_and_forget"] = "await",
        cancel_source: str | None = None,
    ) -> bool:
        """Cancel gateway and AgentServer work for a session.

        Args:
            msg: The gateway message that triggered cancellation.
            old_sid: The session ID whose in-flight work should be cancelled.
            publish_interrupt_result: Whether to publish user-visible interrupt
                results returned by AgentServer. Control commands use this as an
                internal cleanup step, so they suppress the intermediate result
                and only publish their own command notice.
            cancel_gateway_tasks: Whether to cancel in-flight gateway stream tasks.
            agent_notify: ``await`` blocks on AgentServer; ``fire_and_forget`` does not.

        Returns:
            ``True`` when the AgentServer interrupt succeeded (or there was no
            in-flight work to forward); ``False`` when the interrupt request to
            AgentServer failed. Gateway-task cancellation is best-effort and
            does not affect the return value.
        """
        from jiuwenswarm.common.schema.message import Message, ReqMethod

        async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
            if not tasks:
                return
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        self._clear_session_evolution_states(old_sid)

        # 收集需要取消的流式任务（先不取消）
        tasks_to_cancel: list[asyncio.Task] = []
        rids_cancelled: list[str] = []

        for rid, task in list(self._stream_tasks.items()):
            if self._stream_sessions.get(rid) != old_sid:
                continue
            if channel_id is not None and self._stream_channels.get(rid) != channel_id:
                continue
            if not task.done():
                rids_cancelled.append(rid)
                tasks_to_cancel.append(task)

        if old_sid is None and not rids_cancelled:
            return True

        sid_for_agent = (old_sid or "").strip()
        if not sid_for_agent:
            await _cancel_tasks(tasks_to_cancel)
            await self._pop_stream_tracking_and_broadcast(rids_cancelled)
            return True

        # 即使网关侧已无活跃流式拉取任务（例如 Agent 正在执行 shell/工具），也必须通知 AgentServer，
        # 否则仅断开 CLI WebSocket 无法停止已派发的工作。

        # 从 msg.params（已被 _apply_channel_state 注入 mode）或 channel_states
        # 获取 mode 信息，注入到 cancel params 以确保 AgentServer 找到正确的 agent
        cancel_params = {
            "intent": "cancel",
            "session_id": sid_for_agent,
        }
        cancel_mode = None
        if isinstance(msg.params, dict) and msg.params.get("mode"):
            # _apply_channel_state 已将 mode 写入 msg.params
            cancel_mode = msg.params["mode"]
        else:
            # 回退到 channel_states（按被 cancel 的 session，而非触发消息的 session）
            state = self._channel_states.get(
                self._get_channel_state_key(msg.channel_id, sid_for_agent)
            ) or self._channel_states.get(msg.channel_id)
            if state is not None:
                cancel_mode = state.mode.value if hasattr(state.mode, 'value') else str(state.mode)
        if cancel_mode:
            cancel_params["mode"] = cancel_mode
        # 前端 cancel/supplement 请求可能带 team 标识，确保传递到 AgentServer
        # 以便 _handle_cancel 能正确走 team runtime 清理路径
        if isinstance(msg.params, dict) and msg.params.get("team"):
            cancel_params["team"] = msg.params["team"]
        if isinstance(msg.params, dict) and msg.params.get("trusted_dirs"):
            cancel_params["trusted_dirs"] = msg.params["trusted_dirs"]
        cancel_metadata = dict(msg.metadata or {})
        cancel_metadata.pop(E2A_INTERNAL_CANCEL_SOURCE_KEY, None)
        cancel_source_value = (
            cancel_source.strip() if isinstance(cancel_source, str) else ""
        )

        cancel_req = Message(
            id=f"interrupt_{int(time.time() * 1000):x}_{secrets.token_hex(3)}",
            type="req",
            channel_id=msg.channel_id,
            session_id=sid_for_agent,
            params=cancel_params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_CANCEL,
            metadata=cancel_metadata or None,
            provider=getattr(msg, "provider", None),
            chat_id=getattr(msg, "chat_id", None),
            user_id=getattr(msg, "user_id", None),
            bot_id=getattr(msg, "bot_id", None),
        )
        agent_msg = await self._prepare_agent_dispatch_message(cancel_req)
        env_interrupt = self.message_to_e2a(agent_msg)
        if cancel_source_value:
            env_interrupt.channel_context[E2A_INTERNAL_CANCEL_SOURCE_KEY] = cancel_source_value

        if cancel_gateway_tasks:
            await _cancel_tasks(tasks_to_cancel)
            await self._pop_stream_tracking_and_broadcast(rids_cancelled)

        if agent_notify == "fire_and_forget":
            # Still forward cancelled_tools once AgentServer responds — otherwise
            # the UI keeps spinning until refresh (history was written, live push not).
            task = asyncio.create_task(
                self._send_interrupt_to_agent(
                    env_interrupt,
                    channel_id=msg.channel_id,
                    session_id=sid_for_agent,
                    metadata=cancel_metadata or None,
                )
            )
            self._fire_and_forget_tasks.add(task)
            task.add_done_callback(self._fire_and_forget_tasks.discard)
            logger.info(
                "[MessageHandler] 已 fire-and-forget 发送 AgentServer 中断: session_id=%s",
                sid_for_agent,
            )
            if publish_interrupt_result:
                await self._send_interrupt_result_notification(
                    msg.id,
                    msg.channel_id,
                    sid_for_agent,
                    "cancel",
                    success=True,
                )
            return True

        try:
            resp = await self._send_non_stream_agent_request(env_interrupt)
        except Exception as exc:
            logger.warning("[MessageHandler] AgentServer 中断请求失败: %s", exc)
            if cancel_gateway_tasks:
                pass  # gateway tasks already cancelled above
            else:
                await _cancel_tasks(tasks_to_cancel)
                await self._pop_stream_tracking_and_broadcast(rids_cancelled)
            if publish_interrupt_result:
                await self._send_interrupt_result_notification(
                    msg.id, msg.channel_id, sid_for_agent, "cancel",
                    message=f"任务终止失败: {exc}", success=False,
                )
            return False

        if not cancel_gateway_tasks:
            await _cancel_tasks(tasks_to_cancel)
            await self._pop_stream_tracking_and_broadcast(rids_cancelled)

        payload = resp.payload if isinstance(resp.payload, dict) else {}
        if payload.get("event_type") == "chat.interrupt_result":
            if not publish_interrupt_result:
                logger.info(
                    "[MessageHandler] 已静默 AgentServer 中断结果: request_id=%s ok=%s",
                    resp.request_id,
                    resp.ok,
                )
                return bool(resp.ok)
            out = self._response_to_message(
                resp,
                sid_for_agent,
                request_metadata=msg.metadata,
                app_id=msg.app_id or "",
            )
            await self.publish_robot_messages(out)
            logger.info(
                "[MessageHandler] 已转发 AgentServer 中断结果: request_id=%s ok=%s",
                resp.request_id,
                resp.ok,
            )

            # 发送被中断工具的 tool_result 给前端
            await self._send_cancelled_tool_results(
                msg.channel_id, sid_for_agent, payload, msg.metadata
            )
            return bool(resp.ok)

        error_message = "任务终止失败"
        if isinstance(payload, dict):
            raw_error = payload.get("error") or payload.get("message")
            if isinstance(raw_error, str) and raw_error.strip():
                error_message = raw_error.strip()
        elif not resp.ok:
            error_message = "任务终止失败"

        if publish_interrupt_result:
            await self._send_interrupt_result_notification(
                msg.id,
                msg.channel_id,
                sid_for_agent,
                "cancel",
                message=error_message,
                success=False,
            )
        return False

    async def cancel_agent_sessions_on_disconnect(
        self,
        session_keys: list[tuple[str, ...]],
        *,
        stale_request_keys: list[tuple[str, ...]] | None = None,
        user_id: str | None = None,
    ) -> bool:
        """取消仍绑定在断开连接上的会话（与显式 chat.interrupt 对齐）。

        Args:
            session_keys: ``(channel_id, session_id[, agent_ref])`` 元组，来自
                GatewayServer ``_session_to_client`` 中 ``client is ws`` 的反查。
                当用户在同一 ``session_id`` 上重连导致旧 WS 在该映射中被覆盖时，
                这里可能为空。
            stale_request_keys: ``(channel_id, request_id[, agent_ref])`` 元组，来自
                GatewayServer ``_request_to_client`` 中 ``client is ws`` 的反查。即使
                ``session_keys`` 为空，这里仍能让我们通过 ``_stream_sessions``
                找出该 WS 上 in-flight stream 对应的 session_id，避免漏取消。

        Returns:
            ``True`` 表示所有会话的 AgentServer 中断均成功（或无可取消的会话）；
            ``False`` 表示至少有一个会话中断失败。已成功的会话不会被回滚。
        """
        merged, recovered_via_requests = self._merge_disconnect_session_keys(
            session_keys,
            stale_request_keys=stale_request_keys,
        )

        logger.info(
            "[MessageHandler] WS 断开触发 cancel: session_keys=%s recovered_from_requests=%s",
            session_keys,
            recovered_via_requests,
        )

        if not merged:
            return True

        seen: set[str] = set()
        all_cleaned = True
        for _channel_id, session_id in merged:
            sid = (session_id or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            self.cancel_scheduled_disconnect_cancel(_channel_id, sid)
            cleaned = await self._cancel_disconnect_session(_channel_id, sid, user_id=user_id)
            all_cleaned = cleaned and all_cleaned
        return all_cleaned

    async def schedule_cancel_agent_sessions_on_disconnect(
        self,
        session_keys: list[tuple[str, ...]],
        *,
        stale_request_keys: list[tuple[str, ...]] | None = None,
        delay_seconds: float = _TUI_DISCONNECT_CANCEL_GRACE_SECONDS,
        user_id: str | None = None,
    ) -> None:
        """Schedule a disconnect cancel unless the same session reconnects first."""
        merged, recovered_via_requests = self._merge_disconnect_session_keys(
            session_keys,
            stale_request_keys=stale_request_keys,
        )
        logger.info(
            "[MessageHandler] WS 断开延迟 cancel: delay_seconds=%s session_keys=%s recovered_from_requests=%s",
            delay_seconds,
            session_keys,
            recovered_via_requests,
        )
        if not merged:
            return

        seen: set[tuple[str, str]] = set()
        for channel_id, session_id in merged:
            sid = (session_id or "").strip()
            if not sid:
                continue
            task_key = (channel_id, sid)
            if task_key in seen:
                continue
            seen.add(task_key)
            self.cancel_scheduled_disconnect_cancel(channel_id, sid)
            task = asyncio.create_task(
                self._delayed_disconnect_cancel(
                    channel_id, sid, delay_seconds, user_id=user_id
                )
            )
            self._disconnect_cancel_tasks[task_key] = task

    def cancel_scheduled_disconnect_cancel(self, channel_id: str, session_id: str) -> bool:
        """Cancel a pending disconnect-triggered cancel for a reconnected session."""
        sid = (session_id or "").strip()
        if not channel_id or not sid:
            return False
        task = self._disconnect_cancel_tasks.pop((channel_id, sid), None)
        if task is None:
            return False
        if not task.done():
            task.cancel()
        logger.info(
            "[MessageHandler] 已撤销 WS 断开延迟 cancel: channel_id=%s session_id=%s",
            channel_id,
            sid,
        )
        return True

    def _merge_disconnect_session_keys(
        self,
        session_keys: list[tuple[str, ...]],
        *,
        stale_request_keys: list[tuple[str, ...]] | None = None,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        merged: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(channel_id: str, session_id: str) -> bool:
            sid = (session_id or "").strip()
            if not channel_id or not sid:
                return False
            entry = (channel_id, sid)
            if entry in seen:
                return False
            seen.add(entry)
            merged.append(entry)
            return True

        for route_key in session_keys or []:
            if len(route_key) >= 2:
                add(route_key[0], route_key[1])

        recovered_via_requests: list[tuple[str, str]] = []
        for route_key in stale_request_keys or []:
            if len(route_key) < 2:
                continue
            channel_id, request_id = route_key[:2]
            task_session = (self._stream_sessions.get(request_id) or "").strip()
            if add(channel_id, task_session):
                recovered_via_requests.append((channel_id, task_session))

        return merged, recovered_via_requests

    def _build_disconnect_cancel_message(
        self,
        channel_id: str,
        session_id: str,
        user_id: str | None = None,
    ) -> "Message":
        from jiuwenswarm.common.schema.message import Message, ReqMethod

        disconnect_params = {
            "intent": "cancel",
            "session_id": session_id,
        }
        disconnect_state = self._channel_states.get(
            self._get_channel_state_key(channel_id, session_id)
        ) or self._channel_states.get(channel_id)
        if disconnect_state is not None:
            disconnect_params["mode"] = (
                disconnect_state.mode.value
                if hasattr(disconnect_state.mode, "value")
                else str(disconnect_state.mode)
            )
        return Message(
            id=f"ws_drop_{int(time.time() * 1000):x}_{secrets.token_hex(4)}",
            type="req",
            channel_id=channel_id,
            session_id=session_id,
            params=disconnect_params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_CANCEL,
            is_stream=False,
            user_id=user_id,
        )

    async def _cancel_disconnect_session(
        self,
        channel_id: str,
        session_id: str,
        user_id: str | None = None,
    ) -> bool:
        stub = self._build_disconnect_cancel_message(channel_id, session_id, user_id=user_id)
        try:
            return bool(
                await self._cancel_agent_work_for_session(
                    stub,
                    session_id,
                    cancel_source=E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
                )
            )
        except Exception:
            logger.warning(
                "[MessageHandler] disconnect cancel failed: channel_id=%s session_id=%s",
                channel_id,
                session_id,
                exc_info=True,
            )
            return False

    async def _delayed_disconnect_cancel(
        self,
        channel_id: str,
        session_id: str,
        delay_seconds: float,
        user_id: str | None = None,
    ) -> None:
        task_key = (channel_id, session_id)
        try:
            await asyncio.sleep(max(0.0, delay_seconds))
            await self._cancel_disconnect_session(channel_id, session_id, user_id=user_id)
        finally:
            if self._disconnect_cancel_tasks.get(task_key) is asyncio.current_task():
                self._disconnect_cancel_tasks.pop(task_key, None)

    async def _new_session_cancel_and_notice(
        self,
        params: NewSessionCancelParams,
        msg: "Message",
    ) -> None:
        """先完成旧会话取消与 AgentServer 中断，再下发 session 已变更提示。"""
        await self._cancel_agent_work_for_session(
            msg,
            params.old_sid,
            publish_interrupt_result=False,
        )
        await self.send_channel_notice(
            params.user_infos,
            params.channel_id,
            params.reply_session_id,
            f"[收到 CLI 指令], session_id 已变更为 {params.new_sid}",
        )

    async def _mode_change_cancel_and_notice(
        self,
        params: ModeChangeCancelParams,
        msg: "Message",
    ) -> None:
        """与 /new_session 一致：先取消当前会话在网关与 Agent 侧的任务，再下发 mode 已变更提示。"""
        await self._cancel_agent_work_for_session(
            msg,
            params.old_sid,
            publish_interrupt_result=False,
        )
        await self.send_channel_notice(
            params.user_infos,
            params.channel_id,
            params.reply_session_id,
            self._build_mode_change_notice_text(params.new_mode_label),
        )

    @staticmethod
    def _build_mode_change_notice_text(mode_label: str) -> str:
        return f"[收到 CLI 指令], mode 已变更为 {mode_label}"

    def handle_mode_switch(
        self,
        mode_str: str,
        *,
        state: "ChannelControlState",
        user_infos: dict[str, Any] | None = None,
        channel_id: str = "",
        reply_session_id: str | None = None,
        msg: "Message | None" = None,
    ) -> bool:
        """校验并应用 \\mode 指令切换运行模式（P3 抽出，降低 MODE_OK 分支耦合）。

        把原内联在 MODE_OK 分支里的「前置白名单校验 + deprecate_mode 查表分发 +
        通知调度」三段集中到本方法，单一事实源::

            _VALID_MODE_INPUTS = NEW_CANONICAL_MODES | DEPRECATION_MAP.keys()

        - 前置校验：mode_str 不在 ``_VALID_MODE_INPUTS`` 时下发「非法指令」通知并
          返回 True（消息已被消费，无需转发给 Agent）。
        - 分发：``deprecate_mode(mode_str)`` 把旧 canonical 静默映射到新 canonical，
          再用 ``ChannelMode(new_mode_str)`` 直接构造（P2 已加 8 个新成员）。新串
          不在 NEW_CANONICAL_MODES 时兜底为 ``ChannelMode.AGENT``。
        - 通知：mode 实际变更时调度 ``_mode_change_cancel_and_notice``（取消旧
          会话任务 + 下发变更提示），否则下发普通变更提示。``user_infos`` 为 None
          时跳过通知调度（仅供单测直接喂状态用）。

        Returns:
            True：消息已被消费（无论合法与否），调用方 ``return True`` 即可。
        """
        if mode_str not in _VALID_MODE_INPUTS:
            logger.warning(
                "handle_mode_switch: 非法指令 mode_str=%r channel=%s sid=%s",
                mode_str, channel_id, reply_session_id,
            )
            if user_infos is not None:
                asyncio.create_task(
                    self.send_channel_notice(
                        user_infos,
                        channel_id,
                        reply_session_id,
                        "非法指令",
                    )
                )
            return True
        old_mode = state.mode
        old_sid = state.session_id
        state.mode = channel_mode_from_str(mode_str)
        new_label = state.mode.value
        logger.info(
            "handle_mode_switch: channel=%s mode '%s' -> '%s' (old_sid=%s)",
            channel_id, mode_str, new_label, old_sid,
        )
        if user_infos is None:
            return True
        if old_mode != state.mode:
            asyncio.create_task(
                self._mode_change_cancel_and_notice(
                    ModeChangeCancelParams(
                        user_infos=user_infos,
                        channel_id=channel_id,
                        reply_session_id=reply_session_id,
                        old_sid=old_sid,
                        new_mode_label=new_label,
                    ),
                    msg,
                )
            )
        else:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    channel_id,
                    reply_session_id,
                    self._build_mode_change_notice_text(new_label),
                )
            )
        return True

    async def _handle_channel_control(self, msg: "Message") -> bool:
        r"""处理 \new_session / \mode / \skills 指令.

        Returns:
            True: 该消息是控制指令，已处理完毕，不需要转发给 Agent。
            False: 非控制指令，继续正常处理。
        """
        user_infos = {
            "id": msg.id,
            "meta_data": msg.metadata,
            "app_id": getattr(msg, "app_id", None) or getattr(msg, "bot_id", None) or "",
            "bot_id": getattr(msg, "bot_id", None) or "",
        }

        ch = msg.channel_id
        channel_type = self._resolve_control_channel_type(msg)
        if channel_type not in self._control_channel_types:
            return False

        params = msg.params or {}
        text = str(params.get("query") or params.get("content") or "").strip()
        if not text:
            return False

        parsed = parse_channel_control_text(text)
        if parsed.action is ParsedControlAction.NONE:
            return False

        logger.info(
            "[MessageHandler] _handle_channel_control channel=%s text=%s action=%s",
            channel_type,
            text,
            parsed.action.value,
        )

        if parsed.action is ParsedControlAction.SKILLS_OK:
            asyncio.create_task(
                self._skills_slash_notice(user_infos, ch, msg.session_id, msg)
            )
            return True

        # 获取当前会话的状态（使用复合键）
        state = self.get_or_create_channel_state(msg)

        # /join 已认领 team 席位后，除白名单（/exit、只读 /skills list 等）外的控制指令
        # 一律拒绝——执行类指令会改 session/mode/分支/历史或注入 query，破坏席位绑定
        # 或扰乱 team 流程。必须先 /exit。白名单与判定收敛在 JoinExitHandlers，见该类。
        if (
            not self._join_exit.is_allowed_when_joined(parsed.action)
            and self._join_exit.sender_has_joined(msg)
        ):
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    "⚠️ 当前已加入 team session，不能执行该指令。请先执行 **/exit** 退出后再试。",
                )
            )
            return True

        if parsed.action is ParsedControlAction.NEW_SESSION_OK:
            old_sid = state.session_id
            try:
                state.session_id = None
                new_sid = await self._allocate_channel_session(msg, state)
            except Exception:  # noqa: BLE001
                state.session_id = old_sid
                logger.exception(
                    "[MessageHandler] 创建新会话失败 channel=%s message_id=%s",
                    channel_type,
                    msg.id,
                )
                asyncio.create_task(
                    self.send_channel_notice(
                        user_infos,
                        ch,
                        msg.session_id,
                        {"error": "创建新会话失败，请稍后重试"},
                    )
                )
                return True
            # 触发 SessionStart hook
            if self._gateway_hook_handler:
                asyncio.create_task(
                    self._gateway_hook_handler.on_session_start(new_sid, source=channel_type)
                )
            asyncio.create_task(
                self._new_session_cancel_and_notice(
                    NewSessionCancelParams(
                        user_infos=user_infos,
                        channel_id=ch,
                        reply_session_id=msg.session_id,
                        new_sid=new_sid,
                        old_sid=old_sid,
                    ),
                    msg,
                )
            )
            return True
        if parsed.action is ParsedControlAction.PERSIST_OK:
            old_sid = state.session_id
            try:
                state.session_id = None
                new_sid = await self._allocate_channel_session(
                    msg,
                    state,
                    persist_session=True,
                )
            except Exception:  # noqa: BLE001
                state.session_id = old_sid
                logger.exception(
                    "[MessageHandler] 创建永续会话失败 channel=%s message_id=%s",
                    channel_type,
                    msg.id,
                )
                asyncio.create_task(
                    self.send_channel_notice(
                        user_infos,
                        ch,
                        msg.session_id,
                        {"error": "创建永续会话失败，请稍后重试"},
                    )
                )
                return True

            task = parsed.persist_task or ""
            msg.session_id = new_sid
            msg.params = dict(msg.params or {})
            msg.params["query"] = task
            if "content" in msg.params:
                msg.params["content"] = task
            msg.metadata = dict(msg.metadata or {})
            msg.metadata["persist_session_first_task"] = True

            if self._gateway_hook_handler:
                asyncio.create_task(
                    self._gateway_hook_handler.on_session_start(new_sid, source=channel_type)
                )
            asyncio.create_task(
                self._new_session_cancel_and_notice(
                    NewSessionCancelParams(
                        user_infos=user_infos,
                        channel_id=ch,
                        reply_session_id=msg.session_id,
                        new_sid=new_sid,
                        old_sid=old_sid,
                    ),
                    msg,
                )
            )
            # 与 /review 相同：控制动作已经完成，改写后的正文继续作为首条任务。
            return False
        if parsed.action is ParsedControlAction.PERSIST_BAD:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    "指令格式错误。正确格式：/persist <第一条任务>",
                )
            )
            return True
        if parsed.action is ParsedControlAction.NEW_SESSION_BAD:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    "非法指令",
                )
            )
            return True

        if parsed.action is ParsedControlAction.MODE_OK:
            mode_str = parsed.mode_subcommand or ""
            return self.handle_mode_switch(
                mode_str,
                state=state,
                user_infos=user_infos,
                channel_id=ch,
                reply_session_id=msg.session_id,
                msg=msg,
            )
        if parsed.action is ParsedControlAction.SWITCH_OK:
            switch_str = parsed.switch_subcommand or ""
            target_mode: ChannelMode | None = None
            # state.mode 经 handle_mode_switch 落定后必为新 canonical，故判据查新
            # canonical 字符串集合（与旧 ``in (ChannelMode.X, ...)`` 等价，见
            # ``_SWITCH_AGENT_WORK_MODES`` / ``_SWITCH_CODE_MODES``）。
            if switch_str == "plan":
                # agent 下 plan / fast 已合并：/switch plan 保持 agent.work.normal。
                if state.mode.value in _SWITCH_AGENT_WORK_MODES:
                    target_mode = ChannelMode.AGENT_WORK_NORMAL
                elif state.mode.value in _SWITCH_CODE_MODES:
                    target_mode = ChannelMode.AGENT_CODE_PLAN
            elif switch_str == "fast":
                # agent 下 plan / fast 已合并：/switch fast 保持 agent.work.normal。
                if state.mode.value in _SWITCH_AGENT_WORK_MODES:
                    target_mode = ChannelMode.AGENT_WORK_NORMAL
            elif switch_str == "normal":
                if state.mode.value in _SWITCH_CODE_MODES:
                    target_mode = ChannelMode.AGENT_CODE_NORMAL
            elif switch_str == "team":
                if state.mode.value in _SWITCH_CODE_MODES:
                    target_mode = ChannelMode.TEAM_CODE_NORMAL
            if target_mode is None:
                asyncio.create_task(
                    self.send_channel_notice(
                        user_infos,
                        ch,
                        msg.session_id,
                        "非法指令",
                    )
                )
                return True
            old_mode = state.mode
            old_sid = state.session_id
            state.mode = target_mode
            new_label = state.mode.value
            if old_mode != state.mode:
                asyncio.create_task(
                    self._mode_change_cancel_and_notice(
                        ModeChangeCancelParams(
                            user_infos=user_infos,
                            channel_id=ch,
                            reply_session_id=msg.session_id,
                            old_sid=old_sid,
                            new_mode_label=new_label,
                        ),
                        msg,
                    )
                )
            else:
                asyncio.create_task(
                    self.send_channel_notice(
                        user_infos,
                        ch,
                        msg.session_id,
                        self._build_mode_change_notice_text(new_label),
                    )
                )
            return True
        if parsed.action in (ParsedControlAction.MODE_BAD, ParsedControlAction.SWITCH_BAD):
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    "非法指令",
                )
            )
            return True

        if parsed.action is ParsedControlAction.BRANCH_OK:
            asyncio.create_task(
                self._branch_slash_notice(
                    user_infos, ch, msg.session_id, msg,
                    branch_name=parsed.branch_name or "",
                )
            )
            return True

        if parsed.action is ParsedControlAction.REWIND_OK:
            # 两步确认：先发送确认提示，不立即执行
            turn_index = parsed.rewind_turn or 1
            asyncio.create_task(
                self._rewind_slash_confirm_prompt(
                    user_infos, ch, msg.session_id, msg,
                    turn_index=turn_index,
                )
            )
            return True

        if parsed.action is ParsedControlAction.REWIND_CONFIRM:
            # 用户确认执行 rewind
            asyncio.create_task(
                self._rewind_slash_notice(
                    user_infos, ch, msg.session_id, msg,
                    turn_index=parsed.rewind_turn or 1,
                )
            )
            return True

        if parsed.action is ParsedControlAction.REWIND_CANCEL:
            # 用户取消 rewind
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos, ch, msg.session_id,
                    "[收到 /rewind cancel] 已取消回退操作。",
                )
            )
            return True

        if parsed.action is ParsedControlAction.REWIND_BAD:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    "非法指令，/rewind 须带正整数轮次编号，如 /rewind 2",
                )
            )
            return True

        # ── origin/develop: /review / /security-review 指令 ──
        if parsed.action is ParsedControlAction.REVIEW_BAD:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    "非法指令，/review 参数过长或含有非法控制字符",
                )
            )
            return True

        # ── V2: /join / /exit 共享会话指令 ──
        if parsed.action is ParsedControlAction.JOIN_OK:
            asyncio.create_task(
                self._join_exit.join_slash_handler(user_infos, ch, msg, parsed)
            )
            return True
        if parsed.action is ParsedControlAction.JOIN_BAD:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos, ch, msg.session_id,
                    "⚠️ join 指令格式错误",
                )
            )
            return True
        if parsed.action is ParsedControlAction.EXIT_OK:
            asyncio.create_task(
                self._join_exit.exit_slash_handler(user_infos, ch, msg, parsed)
            )
            return True
        if parsed.action is ParsedControlAction.EXIT_BAD:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos, ch, msg.session_id,
                    "exit 指令格式错误。正确格式：/exit <session_id>",
                )
            )
            return True

        # ── origin/develop: /review / /security-review 指令 ──
        if parsed.action is ParsedControlAction.REVIEW_OK:
            # /review [args]：注入 review prompt，转发 Agent 执行 gh pr list/view/diff 并分析
            pr_arg = parsed.pr_arg or ""
            review_prompt = build_review_prompt(pr_arg)
            if msg.params is None:
                msg.params = {}
            msg.params["query"] = review_prompt
            logger.info(
                "[MessageHandler] /review prompt injected channel=%s pr_arg=%s",
                channel_type,
                pr_arg or "<none>",
            )
            return False  # 继续转发给 AgentServer，让 Agent 执行审查

        if parsed.action is ParsedControlAction.SECURITY_REVIEW_BAD:
            asyncio.create_task(
                self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    "非法指令，/security-review 参数过长或含有非法控制字符",
                )
            )
            return True

        if parsed.action is ParsedControlAction.SECURITY_REVIEW_OK:
            extra_arg = parsed.security_review_arg or ""
            cwd = (
                msg.metadata.get("cwd")
                if isinstance(msg.metadata, dict)
                else None
            )
            try:
                # 在注入阶段预执行只读 git 命令并把输出内联进 prompt；
                # 任一命令非零退出（如 origin/HEAD 未设置 / 无共同历史）即中止，
                # 不转发给 Agent。
                # 跑在 executor 里，避免 4×30s 同步 subprocess 阻塞转发循环。
                security_prompt = await asyncio.get_event_loop().run_in_executor(
                    None, build_security_review_prompt, extra_arg, cwd
                )
            except GitPreExecError as exc:
                await self.send_channel_notice(
                    user_infos,
                    ch,
                    msg.session_id,
                    f"/security-review 无法执行：{exc}",
                )
                return True
            if msg.params is None:
                msg.params = {}
            msg.params["query"] = security_prompt
            logger.info(
                "[MessageHandler] /security-review prompt injected channel=%s extra_arg=%s",
                channel_type,
                extra_arg or "<none>",
            )
            return False

        return False

    async def _skills_slash_notice(
        self,
        user_infos: dict[str, Any],
        channel_id: str,
        reply_session_id: str | None,
        msg: "Message",
    ) -> None:
        """受控通道整行 /skills list：请求 skills.list 并以 CHAT_FINAL 通知透传。

        skills.list 响应载荷形如 ``{"skills": [...]}`` / ``{"error": "..."}``，
        不含 ``content`` 字段。多数 IM 通道（微信/钉钉/企微/WhatsApp 等）的 ``send``
        仅从 ``payload.content`` / ``params.content`` 取文本，缺 ``content`` 即被当作
        空消息丢弃，导致 /skills list 无返回（/skills 本身不经此分支故不受影响）。
        因此这里用 ``format_skills_list_for_notice`` 把载荷渲染成纯文本放入 ``content``，
        同时保留原始字段：飞书仍可经 ``_build_skills_list_card_content`` 识别 ``skills``
        键渲染为卡片，其它 IM 通道则回退到读取 ``content`` 文本。
        """
        from jiuwenswarm.common.schema.message import Message, ReqMethod
        from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
            format_skills_list_for_notice,
        )

        req_id = f"skills_slash_{int(time.time() * 1000):x}_{secrets.token_hex(3)}"
        skills_req = Message(
            id=req_id,
            type="req",
            channel_id=msg.channel_id,
            session_id=msg.session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.SKILLS_LIST,
            is_stream=False,
            metadata=msg.metadata,
            provider=getattr(msg, "provider", None),
            chat_id=getattr(msg, "chat_id", None),
            user_id=getattr(msg, "user_id", None),
            bot_id=getattr(msg, "bot_id", None),
        )
        try:
            env = self.message_to_e2a(skills_req)
            resp = await self._send_non_stream_agent_request(env)
            if resp.ok:
                if isinstance(resp.payload, dict):
                    notice_payload: dict[str, Any] = dict(resp.payload)
                else:
                    notice_payload = {"data": resp.payload}
            else:
                err = ""
                if isinstance(resp.payload, dict):
                    err = str(resp.payload.get("error") or "").strip()
                notice_payload = {
                    "error": f"获取技能列表失败{(': ' + err) if err else ''}",
                }
            # 渲染纯文本 content，供只读 content 的 IM 通道（微信等）下发。
            notice_payload["content"] = format_skills_list_for_notice(
                notice_payload if isinstance(notice_payload, dict) else None
            )
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id, notice_payload
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[MessageHandler] /skills list 请求失败: %s", exc)
            await self.send_channel_notice(
                user_infos,
                channel_id,
                reply_session_id,
                {"content": f"获取技能列表失败：{exc}", "error": f"获取技能列表失败：{exc}"},
            )

    async def _branch_slash_notice(
        self,
        user_infos: dict[str, Any],
        channel_id: str,
        reply_session_id: str | None,
        msg: "Message",
        *,
        branch_name: str = "",
    ) -> None:
        """受控通道 /branch：分叉当前会话，切换到新 session 并通知。

        Sends session.fork to AgentServer so both filesystem copy (history.json,
        metadata) and in-memory context copy (DeepAgent checkpointer + context
        engine) are performed atomically.
        """
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        state = self.get_or_create_channel_state(msg)
        source_sid = state.session_id
        if not source_sid:
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                {"error": "当前无活跃会话，无法分叉"},
            )
            return

        channel_type = self._resolve_control_channel_type(msg)

        try:
            env = e2a_from_agent_fields(
                request_id=f"branch-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
                channel_id=channel_type,
                session_id=source_sid,
                req_method=ReqMethod.SESSION_FORK,
                params={
                    "source_session_id": source_sid,
                    "title": branch_name,
                },
                is_stream=False,
                timestamp=time.time(),
            )
            resp = await self._send_non_stream_agent_request(env)
            if not resp.ok:
                payload = dict(resp.payload or {}) if isinstance(resp.payload, dict) else {}
                raise ValueError(str(payload.get("error") or "session.fork failed"))

            result = dict(resp.payload or {}) if isinstance(resp.payload, dict) else {}
            fork_sid = str(result.get("session_id") or "").strip()
            if not fork_sid:
                raise ValueError("session.fork returned empty session_id")
            fork_title = result.get("title", branch_name or "Branched conversation")

            old_sid = state.session_id
            state.session_id = fork_sid

            await self._cancel_agent_work_for_session(msg, old_sid)

            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                f"[收到 /branch 指令] 已分叉会话「{fork_title}」，当前已切换到新会话。",
            )
            logger.info(
                "[MessageHandler] /branch 完成: source=%s fork=%s title=%s",
                source_sid, fork_sid, fork_title,
            )
        except ValueError as e:
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                {"error": f"分叉失败：{e}"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[MessageHandler] /branch 失败: %s", exc)
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                {"error": f"分叉失败：{exc}"},
            )

    async def _rewind_slash_confirm_prompt(
        self,
        user_infos: dict[str, Any],
        channel_id: str,
        reply_session_id: str | None,
        msg: "Message",
        *,
        turn_index: int = 1,
    ) -> None:
        """受控通道 /rewind N：发送确认提示（两步确认第一步）。

        IM 渠道 /rewind 是不可逆操作，需要确认后才执行。
        """
        state = self.get_or_create_channel_state(msg)
        target_sid = state.session_id
        if not target_sid:
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                {"error": "当前无活跃会话，无法回退"},
            )
            return

        await self.send_channel_notice(
            user_infos, channel_id, reply_session_id,
            f"[收到 /rewind {turn_index} 指令] 确认要回退到第 {turn_index} 轮吗？\n"
            f"此操作不可逆，将删除第 {turn_index} 轮及之后的所有对话。\n"
            f"请回复 /rewind confirm {turn_index} 确认，或 /rewind cancel 取消。\n"
            f"注意：回退不影响手动编辑的文件或通过 bash 执行的命令。",
        )

    async def _rewind_slash_notice(
        self,
        user_infos: dict[str, Any],
        channel_id: str,
        reply_session_id: str | None,
        msg: "Message",
        *,
        turn_index: int = 1,
    ) -> None:
        """受控通道 /rewind N：回退当前会话到指定轮次并通知。

        优先转发到 AgentServer（原子性截断 history + context + checkpointer）；
        E2A 不可达时仅对单用户共享目录 client 回退到本地截断 history.json，
        远程/AgentOS client 返回可重试错误（方案 §8：禁止用部署侧目录代替用户目录）。
        """
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client

        state = self.get_or_create_channel_state(msg)
        target_sid = state.session_id
        if not target_sid:
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                {"error": "当前无活跃会话，无法回退"},
            )
            return

        try:
            await self._cancel_agent_work_for_session(msg, target_sid)

            # --- E2A-first: 转发到 AgentServer 原子性处理 ---
            context_ok = False
            try:
                env = e2a_from_agent_fields(
                    request_id=f"rewind-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
                    channel_id=channel_id,
                    session_id=target_sid,
                    req_method=ReqMethod.SESSION_REWIND,
                    params={"session_id": target_sid, "turn_index": turn_index},
                    is_stream=False,
                    timestamp=time.time(),
                )
                resp = await self._send_non_stream_agent_request(env)
                if resp.ok:
                    pl = resp.payload if isinstance(resp.payload, dict) else {}
                    preview = pl.get("content_preview", "")
                    remaining = pl.get("remaining_records", 0)
                    removed = pl.get("removed_records", 0)
                    context_ok = pl.get("rewind_context", False)

                    await self.send_channel_notice(
                        user_infos, channel_id, reply_session_id,
                        f"[收到 /rewind 指令] 已回退到第 {turn_index} 轮"
                        f'（"{preview[:50]}"）'
                        f"，删除 {removed} 条记录，剩余 {remaining} 条。",
                    )
                    logger.info(
                        "[MessageHandler] /rewind 完成(E2A): session=%s turn=%s context=%s",
                        target_sid, turn_index, context_ok,
                    )
                    return
                # AgentServer 已返回业务错误，说明传输可用。远程/AgentOS 模式
                # 不能以“不可达”覆盖真实错误，也不能回退到 Gateway 本地目录。
                error_payload = resp.payload if isinstance(resp.payload, dict) else {}
                logger.warning("[MessageHandler] /rewind E2A failed: %s", error_payload)
                if not is_legacy_shared_directory_client(self.agent_client):
                    await self.send_channel_notice(
                        user_infos, channel_id, reply_session_id,
                        error_payload or {"error": "回退失败"},
                    )
                    return
            except Exception as e2a_exc:
                logger.warning("[MessageHandler] /rewind E2A failed, fallback local: %s", e2a_exc)

            # --- Fallback: 仅单用户共享目录 client 回退到本地截断 history.json ---
            if not is_legacy_shared_directory_client(self.agent_client):
                await self.send_channel_notice(
                    user_infos, channel_id, reply_session_id,
                    {"error": "AgentServer 不可达，无法回退（远程模式不回退本地目录）"},
                )
                return
            from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session
            result = rewind_session(session_id=target_sid, turn_index=turn_index)
            preview = result.get("content_preview", "")
            remaining = result.get("remaining_records", 0)
            removed = result.get("removed_records", 0)

            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                f"[收到 /rewind 指令] 已回退到第 {turn_index} 轮"
                f'（"{preview[:50]}"）'
                f"，删除 {removed} 条记录，剩余 {remaining} 条。"
                f"（注意：上下文未同步截断）",
            )
            logger.info(
                "[MessageHandler] /rewind 完成: session=%s turn=%s remaining=%s removed=%s",
                target_sid, turn_index, remaining, removed,
            )
        except ValueError as e:
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                {"error": f"回退失败：{e}"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[MessageHandler] /rewind 失败: %s", exc)
            await self.send_channel_notice(
                user_infos, channel_id, reply_session_id,
                {"error": f"回退失败：{exc}"},
            )

    def _apply_channel_state(self, msg: "Message") -> None:
        """将当前 Channel 的控制状态应用到消息上（session_id / mode）."""
        channel_type = self._resolve_control_channel_type(msg)
        if channel_type not in self._control_channel_types:
            return

        # V2: 若 resolve_member_by_user 已识别为 team 成员（metadata 含 member_name），
        # 则 session_id 和 mode 已由 handle_message 正确设置，不覆盖。
        if isinstance(msg.metadata, dict) and msg.metadata.get("member_name"):
            # 确保 mode=team，否则 AgentServer 不会走 process_team_message_stream
            if msg.params is None:
                msg.params = {}
            if isinstance(msg.params, dict):
                msg.params["mode"] = "team"
            return

        state = self.get_or_create_channel_state(msg)

        # 仅 _session_map_channel_types 中的通道族使用 SessionMap；其它受控通道仍按 config/state 与入站 session_id。
        cid = str(getattr(msg, "channel_id", "") or "")
        identity_key = self._extract_identity_tuple(msg)
        if identity_key and self._channel_id_matches_session_map_types(cid):
            sid = self._session_map.find_session_id(*identity_key)
            if sid:
                state.session_id = sid
                msg.session_id = sid
            else:
                msg.session_id = None
        elif state.session_id:
            msg.session_id = state.session_id
        else:
            msg.session_id = None

        # 将 mode 写入 params，后续 E2A / Agent 侧从 params["mode"] 读取
        if msg.params is None:
            msg.params = {}
        if isinstance(msg.params, dict):
            msg.params.setdefault("mode", state.mode.value)

    # ---------- user_messages ----------

    async def publish_user_messages(self, msg: "Message") -> None:
        """将消息放入 user_messages 队列（异步）."""
        await self._user_messages.put(msg)

    def publish_user_messages_nowait(self, msg: "Message") -> None:
        """将消息放入 user_messages 队列（同步）."""
        self._user_messages.put_nowait(msg)

    async def consume_user_messages(self, timeout: float | None = None) -> "Message | None":
        """消费一条 user_messages；timeout 为 None 则阻塞，否则超时返回 None."""
        if timeout is not None and timeout <= 0:
            try:
                return self._user_messages.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            if timeout is None:
                return await self._user_messages.get()
            return await asyncio.wait_for(self._user_messages.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ---------- robot_messages ----------

    def _is_chat_stream(self, rid: str) -> bool:
        """``rid`` 是否为计入运行态的对话流（``chat.send``）。

        计数口径与 ``_should_emit_processing_status_for_stream`` 对齐：只有
        会发 processing_status 的流式任务（emit=True，即 chat.send）算“对话
        任务在跑”。``history.get`` / ``chat.error`` 等只读/异常流登记在
        ``_stream_modes`` 供取消取 mode 与 cron 回填 mode 用，但**不计入运行态**，
        否则它们退出时会让保存锁计数悬空在最后一次广播值。emit 标志在启动时
        先于 mode 写入、两者成对 pop（见 ``_pop_stream_tracking``），故
        ``_stream_modes`` 中的 rid 必有对应标志项。
        """
        return bool(self._stream_emits_processing_status.get(rid))

    def _iter_active_stream_modes(self) -> list[str]:
        """计入运行态的活跃对话任务 mode 快照（流式 chat + 非流式 chat）。

        流式只取对话流（见 ``_is_chat_stream``），``_active_chat_tasks`` 天然
        都是对话任务，二者合并即覆盖全部“需触发配置保存锁”的任务。
        """
        return [
            mode
            for rid, mode in self._stream_modes.items()
            if self._is_chat_stream(rid)
        ] + [*self._active_chat_tasks.values()]

    def has_active_streams(self) -> bool:
        """是否有非 team 模式的用户对话任务正在运行（配置保存锁用，只读）。

        team 任务用隔离 cron_* 会话、不受配置热更新影响，故配置保存锁
        （前端禁用 + ack task_running + 后端拒绝）只看非 team 任务。
        覆盖流式（``_stream_modes``）与非流式 chat（``_active_chat_tasks`` 存 mode）。

        default fallback mode（如 ``"plan"``）经 ``is_team_mode`` 返回 False，
        不会误判为 team，故历史无 mode 的路径仍计入锁。
        """
        return any(
            not ChannelMode.is_team_mode(mode)
            for mode in self._iter_active_stream_modes()
        )

    def active_non_team_modes(self) -> list[tuple[str, str]]:
        """非 team 活跃对话任务的 (request_id, mode) 明细（只读，调试/日志用）。

        口径与 ``_iter_active_stream_modes`` 一致，再按 team 排除。
        供保存锁拒绝日志定位“是哪个 rid 让运行态误判为 true”。
        """
        result: list[tuple[str, str]] = []
        for rid, mode in self._stream_modes.items():
            if self._is_chat_stream(rid) and not ChannelMode.is_team_mode(mode):
                result.append((rid, mode))
        for rid, mode in self._active_chat_tasks.items():
            if not ChannelMode.is_team_mode(mode):
                result.append((rid, mode))
        return result

    async def _broadcast_task_global_running(self) -> None:
        """向所有 web ws 客户端广播当前全局运行态快照（task.global_running）。

        running/count 只计非 team 任务（team 任务不受配置热更新影响，不触发
        配置保存锁），保证多任务并发时计数准确、幂等。
        任何 chat 任务的起止点都应调用，让跨窗口配置保存锁感知运行态变化。
        """
        try:
            web_channel = self._resolve_web_channel()
            if web_channel is not None:
                all_modes = self._iter_active_stream_modes()
                count = sum(
                    1 for mode in all_modes
                    if not ChannelMode.is_team_mode(mode)
                )
                # 非团队活跃 mode 明细，供排查“保存锁卡禁用”时定位是哪个 rid 残留。
                non_team_modes = [m for m in all_modes if not ChannelMode.is_team_mode(m)]
                team_modes = [m for m in all_modes if ChannelMode.is_team_mode(m)]
                await web_channel.broadcast_event("task.global_running", {
                    "event_type": "task.global_running",
                    "running": bool(count),
                    "count": count,
                })
                # chat_stream_rids 计入运行态的对话流数；stream_rids 为注册流总数
                # （含 history.get 等 emit=False 非对话流），故前者 <= 后者。
                chat_stream_rids = sum(
                    1 for rid in self._stream_modes if self._is_chat_stream(rid)
                )
                logger.info(
                    "[task.global_running] broadcast: running=%s non_team_count=%d "
                    "non_team_modes=%s team_modes=%s chat_stream_rids=%d "
                    "stream_rids=%d active_chat_rids=%d",
                    bool(count), count, non_team_modes, team_modes, chat_stream_rids,
                    len(self._stream_modes), len(self._active_chat_tasks),
                )
            else:
                logger.info(
                    "[task.global_running] skip broadcast: web channel 未就绪 "
                    "(stream_rids=%d active_chat_rids=%d)",
                    len(self._stream_modes), len(self._active_chat_tasks),
                )
        except Exception:
            logger.debug("[task.global_running] broadcast failed", exc_info=True)

    def set_channel_manager(self, channel_manager: Any) -> None:
        """注入 ChannelManager 引用，供广播全局事件（如 task.global_running）取 web channel 用。

        MessageHandler 在 ChannelManager 之前实例化（ChannelManager 构造需要 message_handler），
        故通过此方法在装配阶段回填引用。
        """
        self._channel_manager = channel_manager

    def _resolve_web_channel(self) -> Any:
        """从 ChannelManager 取 web channel 实例（广播用）；取不到返回 None。"""
        cm = getattr(self, "_channel_manager", None)
        if cm is None:
            return None
        try:
            return cm.get_channel("web")
        except Exception:
            return None

    async def publish_robot_messages(self, msg: "Message") -> None:
        """将 Agent 响应放入 robot_messages 队列."""
        # Outbound Pipeline（数字分身出站路由）— 在入队前运行
        if self._outbound_pipeline is not None:
            try:
                await self._outbound_pipeline.apply(msg)
            except Exception:
                logger.exception("Outbound pipeline error, message queued without routing")
        await self._robot_messages.put(msg)

    def publish_robot_messages_nowait(self, msg: "Message") -> None:
        """将 Agent 响应放入 robot_messages 队列（同步）."""
        self._robot_messages.put_nowait(msg)

    async def consume_robot_messages(self, timeout: float | None = None) -> "Message | None":
        """消费一条 robot_messages；timeout 为 None 则阻塞，否则超时返回 None."""
        if timeout is not None and timeout <= 0:
            try:
                return self._robot_messages.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            if timeout is None:
                return await self._robot_messages.get()
            return await asyncio.wait_for(self._robot_messages.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    @staticmethod
    def _is_session_map_style_session_id(session_id: str) -> bool:
        parts = [part.strip() for part in str(session_id or "").split("::")]
        if len(parts) not in (5, 6):
            return False
        return all(parts)

    @classmethod
    def _is_known_jiuwenswarm_session_id(cls, session_id: str | None) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        if sid.startswith(_KNOWN_JIUWENSWARM_SESSION_PREFIXES):
            return True
        return cls._is_session_map_style_session_id(sid)

    async def _ensure_acp_agent_session(self) -> str:
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        env = e2a_from_agent_fields(
            request_id=f"acp-session-create-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
            channel_id=_ACP_CHANNEL_ID,
            req_method=ReqMethod.SESSION_CREATE,
            params={
                "create_token": secrets.token_hex(16),
                "mode": "agent",
                "is_swarm": False,
            },
            is_stream=False,
            timestamp=time.time(),
        )
        resp = await self._send_non_stream_agent_request(env)
        if not resp.ok:
            payload = dict(resp.payload or {}) if isinstance(resp.payload, dict) else {}
            raise RuntimeError(str(payload.get("error") or "acp session.create failed"))
        payload = dict(resp.payload or {}) if isinstance(resp.payload, dict) else {}
        resolved = payload.get("sessionId") or payload.get("session_id")
        resolved_str = str(resolved or "").strip()
        if not resolved_str:
            raise RuntimeError("acp session.create returned empty session_id")
        return resolved_str

    async def _resolve_acp_internal_session_id(
        self,
        external_session_id: str | None,
    ) -> tuple[str | None, bool]:
        external = str(external_session_id or "").strip()
        if not external:
            return None, False

        cached = self._acp_session_aliases.get(external)
        if cached:
            return cached, cached != external

        async with self._acp_session_alias_lock:
            cached = self._acp_session_aliases.get(external)
            if cached:
                return cached, cached != external

            ensured = await self._ensure_acp_agent_session()
            self._acp_session_aliases[external] = ensured
            return ensured, ensured != external

    async def _prepare_agent_dispatch_message(self, msg: "Message") -> "Message":
        from jiuwenswarm.common.schema.message import ReqMethod

        msg = self._attach_original_request_to_ask_user_answer(msg)
        if msg.channel_id != _ACP_CHANNEL_ID:
            return msg
        if msg.req_method in (ReqMethod.INITIALIZE, ReqMethod.SESSION_CREATE):
            return msg

        internal_session_id, aliased = await self._resolve_acp_internal_session_id(msg.session_id)
        if not internal_session_id:
            return msg

        params = dict(msg.params or {})
        params["session_id"] = internal_session_id

        metadata = dict(msg.metadata or {})
        if aliased:
            metadata.setdefault(_ACP_ORIGINAL_SESSION_ID_KEY, str(msg.session_id or ""))

        return replace(
            msg,
            session_id=internal_session_id,
            params=params,
            metadata=metadata or None,
        )

    def _resolve_acp_external_session_id(
        self,
        session_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None

        original = ""
        if isinstance(metadata, dict):
            original = str(metadata.get(_ACP_ORIGINAL_SESSION_ID_KEY) or "").strip()
        if original:
            return original

        for external, internal in self._acp_session_aliases.items():
            if internal == sid:
                return external
        return sid

    @staticmethod
    def resolve_at_file_references(
        content: str,
        cwd: str | None = None,
        max_file_size: int | None = _DEFAULT_INLINE_FILE_SIZE_LIMIT,
    ) -> str:
        """Parse ``@path`` references in *content* and inline the file text.

        Supported forms:
        - ``@relative/path`` / ``@/absolute/path`` — resolved against *cwd*
        - ``@"path with spaces"`` — quoted paths
        - ``@path#L10-20`` — line-range suffix (ignored for now, whole file read)

        Returns content with ``@path`` replaced by a ``<file-content>`` block
        containing the actual text.  If a file cannot be read the original
        ``@path`` is kept unchanged.
        """
        if not content:
            return content

        working_dir = cwd or os.getcwd()

        # Match @path or @"quoted path", optionally followed by #L... line range
        pattern = re.compile(
            r'(?P<prefix>(?:^|(?<=\s)))@(?:"(?P<quoted>[^"]+)"|(?P<plain>[^\s#]+))(?:#[^#\s]*)?'
        )

        def _replacer(m: re.Match[str]) -> str:
            raw = m.group("quoted") or m.group("plain") or ""
            if not raw:
                return m.group(0)

            # Skip @agent-xxx mentions (not file references)
            if raw.startswith("agent-") or raw.startswith("agent:"):
                return m.group(0)

            # Resolve path
            if raw.startswith("~/"):
                home = os.path.expanduser("~")
                resolved = os.path.join(home, raw[2:])
            elif MessageHandler._is_absolute_reference_path(raw):
                resolved = raw
            else:
                resolved = os.path.join(working_dir, raw)

            try:
                path = Path(resolved)
                if not path.is_file():
                    return m.group(0)
                size = path.stat().st_size
                truncated = False
                if max_file_size is None:
                    text = path.read_text(encoding="utf-8", errors="replace")
                else:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        text = handle.read(max_file_size + 1)
                    if size > max_file_size or len(text) > max_file_size:
                        truncated = True
                    if len(text) > max_file_size:
                        text = text[:max_file_size]
                    if truncated:
                        suffix = f"\n... (truncated, original_size={size} bytes)"
                        text = f"{text}{suffix}"
                return (
                    f'\n<file-content path="{raw}">\n{text}\n</file-content>\n'
                )
            except (OSError, UnicodeDecodeError):
                return m.group(0)

        return pattern.sub(_replacer, content)

    @staticmethod
    def extract_agent_mentions(content: str) -> list[str]:
        """Parse ``@agent-xxx`` and ``@"xxx (agent)"`` mentions from content.

        Returns list of agent type names (without "agent-" prefix), deduplicated.
        """
        if not content:
            return []

        results: list[str] = []

        # Match quoted format: @"<type> (agent)"
        for m in re.finditer(r'(^|\s)@"([\w:.@-]+)\s+\(agent\)"', content):
            results.append(m.group(2))

        # Match unquoted format: @agent-<type>
        for m in re.finditer(r'(^|\s)@(agent-[\w:.@-]+)', content):
            full = m.group(2)
            results.append(full[len("agent-"):])

        # Deduplicate preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for name in results:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique

    @staticmethod
    def _is_absolute_reference_path(raw: str) -> bool:
        return raw.startswith("/") or (len(raw) >= 3 and raw[1] == ":" and raw[2] == "\\")

    @staticmethod
    def _resolve_reference_path(raw: str, cwd: str | None = None) -> str:
        working_dir = cwd or os.getcwd()
        if raw.startswith("~/"):
            return os.path.join(os.path.expanduser("~"), raw[2:])
        if MessageHandler._is_absolute_reference_path(raw):
            return raw
        return os.path.join(working_dir, raw)

    @classmethod
    def _normalize_structured_attachments(
        cls,
        attachments: Any,
        cwd: str | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(attachments, list):
            return []

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in attachments:
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or "").strip()
            if not raw_path:
                continue
            resolved_path = cls._resolve_reference_path(raw_path, cwd)
            if resolved_path in seen:
                continue
            seen.add(resolved_path)
            normalized.append(
                {
                    "path": resolved_path,
                    "type": str(item.get("type") or "file").strip() or "file",
                    "filename": str(item.get("filename") or Path(resolved_path).name).strip(),
                }
            )
        return normalized

    @classmethod
    def strip_attached_mentions(
        cls,
        content: str,
        attachments: list[dict[str, Any]],
        cwd: str | None = None,
    ) -> str:
        if not content or not attachments:
            return content

        attached_paths = {
            cls._resolve_reference_path(str(item.get("path") or ""), cwd)
            for item in attachments
            if str(item.get("path") or "").strip()
        }
        if not attached_paths:
            return content

        pattern = re.compile(
            r'(?P<prefix>(?:^|(?<=\s)))@(?:"(?P<quoted>[^"]+)"|(?P<plain>[^\s#]+))(?:#[^#\s]*)?'
        )

        def _replacer(match: re.Match[str]) -> str:
            raw = match.group("quoted") or match.group("plain") or ""
            if not raw:
                return match.group(0)
            # Skip @agent-xxx mentions (not file references)
            if raw.startswith("agent-") or raw.startswith("agent:"):
                return match.group(0)
            resolved = cls._resolve_reference_path(raw, cwd)
            if resolved not in attached_paths:
                return match.group(0)
            return f"{match.group('prefix')}{raw}"

        return pattern.sub(_replacer, content)

    @classmethod
    def _resolve_structured_attachments(
        cls,
        content: str,
        attachments: Any,
        cwd: str | None = None,
    ) -> str:
        normalized = cls._normalize_structured_attachments(attachments, cwd)
        if not normalized:
            return content

        prefix = " ".join(f'@"{item["path"]}"' for item in normalized)
        cleaned_content = cls.strip_attached_mentions(content, normalized, cwd)
        merged_content = f"{prefix} {cleaned_content}".strip()
        return cls.resolve_at_file_references(merged_content, cwd=cwd)

    @staticmethod
    def message_to_e2a(msg: "Message") -> "E2AEnvelope":
        from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a_or_fallback

        return message_to_e2a_or_fallback(msg)


    @staticmethod
    def _merge_agent_metadata(
        request_metadata: dict[str, Any] | None,
        response_metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """合并 Agent 响应 metadata 与网关请求 metadata。

        send_push / 工具链返回的响应常不带 metadata，通道（如钉钉 batchSend）需要
        请求侧的 dingtalk_sender_id、conversation_type 等；响应中有同名字段时优先响应。
        """
        req_md = request_metadata or {}
        resp_md = response_metadata or {}
        if not req_md and not resp_md:
            return None
        merged: dict[str, Any] = {**req_md, **resp_md}
        for key in _DELIVERY_IDENTITY_METADATA_KEYS:
            if key in req_md:
                merged[key] = req_md[key]
        return merged

    @staticmethod
    def _response_to_message(
        resp: "AgentResponse",
        session_id: str | None,
        *,
        request_metadata: dict[str, Any] | None = None,
        app_id: str | None = None,
    ) -> "Message":
        from jiuwenswarm.common.schema.message import Message, EventType

        metadata = MessageHandler._merge_agent_metadata(request_metadata, resp.metadata)

        # ── V2: 合并 resp 的 agent_ref ──
        resp_agent_ref = getattr(resp, "agent_ref", None)

        # 从 metadata 中提取 group_digital_avatar 和 enable_memory 字段
        # 这些字段在 message_to_e2a 中被放入 metadata，需要在这里提取出来
        group_digital_avatar = bool(metadata.get("group_digital_avatar", False)) if metadata else False
        enable_memory = bool(metadata.get("enable_memory", True)) if metadata else True

        # 检查 payload 中是否包含 event_type，如果包含则创建事件消息
        event_type = None
        payload = resp.payload
        if resp.payload and isinstance(resp.payload, dict):
            payload = apply_a2ui_text_fallback_to_gateway_payload(
                dict(resp.payload),
                channel_id=resp.channel_id,
            )
            payload = normalize_legacy_health_check_relay_payload(payload)
            event_type_str = payload.get("event_type")
            if isinstance(event_type_str, str):
                try:
                    event_type = EventType(event_type_str)
                    # 如果是事件类型，创建事件消息而不是响应消息
                    return Message(
                        id=resp.request_id,
                        type="event",
                        channel_id=resp.channel_id,
                        session_id=session_id,
                        params={},
                        timestamp=time.time(),
                        ok=True,
                        payload=payload,
                        event_type=event_type,
                        metadata=metadata,
                        app_id=app_id,
                        agent_ref=resp_agent_ref,
                        group_digital_avatar=group_digital_avatar,
                        enable_memory=enable_memory,
                    )
                except ValueError:
                    # 不是有效的 EventType，继续作为普通响应处理
                    pass

        # 普通响应消息
        return Message(
            id=resp.request_id,
            type="res",
            channel_id=resp.channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=resp.ok,
            payload=payload,
            event_type=EventType.CHAT_FINAL,
            metadata=metadata,
            app_id=app_id,
            agent_ref=resp_agent_ref,
            group_digital_avatar=group_digital_avatar,
            enable_memory=enable_memory,
        )

    async def _handle_agent_server_push(self, wire: dict[str, Any]) -> None:
        """AgentServer ``send_push`` 下行：与 RPC 共用连接但不得占用 unary/stream 等待队列。"""
        from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk

        try:
            chunk = parse_agent_server_wire_chunk(wire)
        except Exception as e:
            logger.exception("[MessageHandler] server_push 解析失败: %s", e)
            return
        rid = str(chunk.request_id or "")
        sid_raw = wire.get("session_id")
        if sid_raw is not None and str(sid_raw).strip():
            session_id: str | None = str(sid_raw)
        else:
            session_id = self._stream_sessions.get(rid)

        if await self._handle_trajectory_update_push(chunk, session_id):
            return
        
        # 获取原始请求的 metadata，用于合并
        request_metadata = self._stream_metadata.get(rid)
        
        # 获取 AgentServer 返回的 metadata
        wmd = wire.get("metadata")
        if isinstance(wmd, dict):
            resp_md = {
                k: v
                for k, v in wmd.items()
                if k not in E2A_WIRE_INTERNAL_METADATA_KEYS
            }
        else:
            resp_md = None

        # 合并 metadata：请求 metadata 在前，响应 metadata 在后（响应优先）
        bus_metadata = MessageHandler._merge_agent_metadata(request_metadata, resp_md)

        if chunk.channel_id == _ACP_CHANNEL_ID:
            session_id = self._resolve_acp_external_session_id(session_id, bus_metadata)
        if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "cron.response":
            # Cron tool mutations are server-push frames.  They may be handled
            # after the chat stream finalizer has removed ``_stream_user_ids``.
            # CronTools therefore carries this internal value from the
            # authenticated AgentServer request context.  Prefer the live
            # Gateway mapping when it still exists; reject a disagreement rather
            # than letting a stale/malformed frame cross user boundaries.
            mapped_owner = str(self._stream_user_ids.get(rid) or "").strip()
            wire_metadata = wire.get("metadata")
            pushed_owner = (
                str(wire_metadata.get("_jiuwenswarm_cron_owner_user_id") or "").strip()
                if isinstance(wire_metadata, dict)
                else ""
            )
            if mapped_owner and pushed_owner and mapped_owner != pushed_owner:
                logger.warning(
                    "[MessageHandler] discard cron push with mismatched owner: request_id=%s",
                    rid,
                )
                return
            await self._handle_cron_push_payload(
                payload=dict(chunk.payload),
                request_id=rid,
                channel_id=chunk.channel_id,
                session_id=session_id,
                metadata=bus_metadata,
                user_id=mapped_owner or pushed_owner or None,
            )
            return
        if self._is_terminal_stream_chunk(chunk):
            logger.debug(
                "[MessageHandler] 忽略 server_push 终止 chunk: request_id=%s",
                chunk.request_id,
            )
            return

        # Track evolution state on the server_push path as well.
        if not await self._handle_evolution_chunk(chunk, session_id, bus_metadata):
            return

        # 多应用：push 重建 Message 时补 app_id，否则 _resolve_app_id 兜底 "default"，
        # channel_manager 精确路由 ChannelKey(channel_id, "default") 在多 app 场景拿不到
        # 正确 channel 实例（feishu config.app_id 非空时注册 key 是真实 app_id），
        # 退到 _get_channel_by_id 扫描会命中第一个实例，导致 send_user_file 等工具 push
        # 投递到错误 app 的 channel（config.chat_id/last_chat_id 不符）→ 文件发错人或发不出。
        # 流式请求取 _stream_app_ids[rid]（与 process_stream 一致）；非流式请求
        # _stream_app_ids 未注册，回退 request_metadata["app_id"]（入站已透传）。
        _push_app_id = (
            self._stream_app_ids.get(rid, "")
            or (request_metadata.get("app_id") if isinstance(request_metadata, dict) else "")
            or (resp_md.get("app_id") if isinstance(resp_md, dict) else "")
        )
        out = self._chunk_to_message(
            chunk, session_id=session_id, metadata=bus_metadata,
            app_id=_push_app_id,
        )
        await self.publish_robot_messages(out)
        logger.info(
            "[MessageHandler] server_push 已写入 robot_messages: request_id=%s channel_id=%s app_id=%s",
            rid,
            chunk.channel_id,
            _push_app_id,
        )

    async def _handle_trajectory_update_push(
        self,
        chunk: Any,
        session_id: str | None,
    ) -> bool:
        """Forward one AgentServer trajectory watermark to the browser channel."""
        payload = chunk.payload
        if not isinstance(payload, dict) or payload.get("event_type") != "trace.updated":
            return False
        resolved_session_id = str(payload.get("session_id") or session_id or "").strip()
        trace_id = str(payload.get("trace_id") or "").strip()
        if not resolved_session_id or not trace_id:
            logger.warning("[MessageHandler] invalid trajectory update push ignored")
            return True
        try:
            revision = int(payload.get("revision", 0))
        except (TypeError, ValueError, OverflowError):
            logger.warning("[MessageHandler] invalid trajectory update revision ignored")
            return True
        if revision < 1:
            logger.warning("[MessageHandler] non-positive trajectory update revision ignored")
            return True
        web_channel = self._resolve_web_channel()
        if web_channel is None:
            logger.debug(
                "[MessageHandler] trajectory update skipped because web channel is unavailable"
            )
            return True
        from jiuwenswarm.observability.models import CommittedTraceUpdate

        web_channel.schedule_trajectory_updates(
            (
                CommittedTraceUpdate(
                    session_id=resolved_session_id,
                    trace_id=trace_id,
                    revision=revision,
                    store_epoch=payload.get("store_epoch"),
                    lifecycle=str(payload.get("lifecycle") or "final"),
                ),
            )
        )
        return True

    def set_cron_controller(self, controller: Any) -> None:
        self._cron_controller = controller

    async def _handle_cron_push_payload(
        self,
        *,
        payload: dict[str, Any],
        request_id: str,
        channel_id: str,
        session_id: str | None,
        metadata: dict[str, Any] | None,
        user_id: str | None = None,
    ) -> None:
        cc = self._cron_controller
        if cc is None:
            return
        action = str(payload.get("action") or "").strip()
        command_id = str(payload.get("command_id") or "").strip()
        params = payload.get("data") or {}
        if not isinstance(params, dict):
            params = {}
        from jiuwenswarm.gateway.routing.e2a_proxy import is_agentos_routing_client

        is_agentos = is_agentos_routing_client(self.agent_client)
        owner_user_id = str(user_id or "").strip()

        async def _get_owned_job(job_id: str) -> dict[str, Any] | None:
            job = await cc.get_job(job_id)
            if job is None:
                return None
            job_owner = (
                job.get("user_id", "")
                if isinstance(job, dict)
                else getattr(job, "user_id", "")
            )
            if owner_user_id and str(job_owner or "").strip() != owner_user_id:
                return None
            return job

        try:
            if action == "list":
                data = await cc.list_jobs()
                if owner_user_id:
                    data = [
                        job
                        for job in data
                        if str(job.get("user_id") or "").strip() == owner_user_id
                    ]
            elif action == "get":
                data = await _get_owned_job(str(params.get("job_id") or ""))
                if data is None:
                    raise KeyError("job not found")
            elif action == "create":
                # Gateway, rather than an AgentServer payload, is authoritative
                # for the authenticated owner in AgentOS.
                if owner_user_id:
                    params["user_id"] = owner_user_id
                # 从原始请求中获取 mode，覆盖 LLM 工具调用的默认值
                request_mode = self._stream_modes.get(request_id)
                if request_mode:
                    params["mode"] = request_mode
                # project 已在 AgentServer 的用户目录中完成解析；Gateway 侧目录
                # 与用户目录隔离时，允许 controller 跳过其本地反查。
                if is_agentos:
                    params["_agentos_project_binding_verified"] = True
                data = await cc.create_job(params)
            elif action == "update":
                job_id = str(params.get("job_id") or "")
                if await _get_owned_job(job_id) is None:
                    raise KeyError("job not found")
                patch = dict(params.get("patch") or {})
                if is_agentos:
                    patch["_agentos_project_binding_verified"] = True
                data = await cc.update_job(job_id, patch)
            elif action == "delete":
                job_id = str(params.get("job_id") or "")
                if await _get_owned_job(job_id) is None:
                    raise KeyError("job not found")
                data = {"deleted": await cc.delete_job(job_id)}
            elif action == "toggle":
                job_id = str(params.get("job_id") or "")
                if await _get_owned_job(job_id) is None:
                    raise KeyError("job not found")
                data = await cc.toggle_job(job_id, bool(params.get("enabled")))
            elif action == "preview":
                job_id = str(params.get("job_id") or "")
                if await _get_owned_job(job_id) is None:
                    raise KeyError("job not found")
                data = await cc.preview_job(job_id, int(params.get("count", 5)))
            elif action == "run_now":
                job_id = str(params.get("job_id") or "")
                if await _get_owned_job(job_id) is None:
                    raise KeyError("job not found")
                data = {"run_id": await cc.run_now(job_id)}
                # P2：把 Gateway 生成的 run_id 经 E2A 回传目标 AgentServer，让
                # CronTools.run_now 能拿到非空 run_id（异步确认；失败不影响下方
                # 面向 web 的 chat.tool_result 推送，AgentServer 侧等待方超时降级）。
                await self._push_cron_run_now_ack(
                    request_id=request_id,
                    job_id=str(params.get("job_id") or ""),
                    run_id=str(data.get("run_id") or ""),
                    user_id=owner_user_id or None,
                )
            else:
                data = {"error": f"unknown cron action: {action}"}
        except Exception as exc:  # noqa: BLE001
            data = {"error": str(exc)}

        if command_id:
            await self._push_cron_command_ack(
                command_id=command_id,
                data=data,
                user_id=owner_user_id or None,
            )

        from jiuwenswarm.common.schema.message import EventType, Message
        out = Message(
            id=request_id,
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": "chat.tool_result",
                "tool_name": "cron",
                "result": data,
            },
            event_type=EventType.CHAT_TOOL_RESULT,
            metadata=metadata,
            enable_streaming=False,  # 工具结果不开启流式，避免被发送到群聊
        )
        await self.publish_robot_messages(out)

    async def _push_cron_command_ack(
        self, *, command_id: str, data: Any, user_id: str | None = None
    ) -> None:
        """Return a Gateway cron command result to the waiting Agent tool."""
        try:
            from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenswarm.common.schema.message import ReqMethod

            env = e2a_from_agent_fields(
                request_id=f"cron-command-ack-{command_id}", channel_id="",
                req_method=ReqMethod.CRON_COMMAND_ACK,
                params={"command_id": command_id, "data": data},
                is_stream=False, timestamp=time.time(), user_id=user_id,
            )
            await send_agent_request_with_timeout(
                self.agent_client, env, label="cron.command.ack"
            )
        except Exception:
            logger.warning("[Cron] command ack delivery failed: command_id=%s", command_id, exc_info=True)

    async def _push_cron_run_now_ack(
        self, *, request_id: str, job_id: str, run_id: str, user_id: str | None = None
    ) -> None:
        """把 Gateway 生成的 run_id 经 E2A 回传目标 AgentServer（P2）。

        AgentServer 侧 ``CronTools.run_now`` 按 request_id 等待该确认；回传失败
        时 AgentServer 侧等待方超时降级，不影响本方法调用方（web 推送）。
        """
        if not request_id:
            return
        try:
            from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenswarm.common.schema.message import ReqMethod

            env = e2a_from_agent_fields(
                request_id=f"cron-run-ack-{request_id}",
                channel_id="",
                req_method=ReqMethod.CRON_RUN_NOW_ACK,
                params={
                    "ack_request_id": request_id,
                    "job_id": job_id,
                    "run_id": run_id,
                },
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id,
            )
            await send_agent_request_with_timeout(
                self.agent_client,
                env,
                label="cron.run_now.ack",
            )
        except Exception as exc:  # noqa: BLE001 - ack failure degrades to timeout
            logger.warning(
                "[Cron] run_now ack push failed: request_id=%s error=%s", request_id, exc
            )

    @staticmethod
    def _chunk_to_message(
        chunk: AgentResponseChunk,
        session_id: str | None,
        *,
        metadata: dict[str, Any] | None = None,
        app_id: str | None = None,
    ) -> Message:
        """将 AgentResponseChunk 转换为 Message（用于流式处理）。
        metadata 传入 request 的 metadata，供 Feishu/Xiaoyi 等通道回发时使用平台身份。
        """
        from jiuwenswarm.common.schema.message import Message, EventType

        # 从 metadata 中提取 group_digital_avatar 和 enable_memory 字段
        # 这些字段在 message_to_e2a 中被放入 metadata，需要在这里提取出来
        group_digital_avatar = bool(metadata.get("group_digital_avatar", False)) if metadata else False
        enable_memory = bool(metadata.get("enable_memory", True)) if metadata else True

        # ── V2: 合并 chunk.metadata（含 fan_out_targets）+ chunk.agent_ref ──
        chunk_md = getattr(chunk, "metadata", None) or {}
        merged_metadata: dict[str, Any] | None = None
        if metadata or chunk_md:
            merged_metadata = {**(metadata or {}), **chunk_md}

        chunk_agent_ref = getattr(chunk, "agent_ref", None)

        # 从 payload 中提取 event_type（如果存在）
        event_type = None
        payload = chunk.payload
        if chunk.payload and isinstance(chunk.payload, dict):
            payload = apply_a2ui_text_fallback_to_gateway_payload(
                dict(chunk.payload),
                channel_id=chunk.channel_id,
            )
            payload = normalize_legacy_health_check_relay_payload(payload)
            event_type_str = payload.get("event_type")
            if isinstance(event_type_str, str):
                try:
                    event_type = EventType(event_type_str)
                except ValueError:
                    logger.debug("未知的 event_type: %s", event_type_str)

        return Message(
            id=chunk.request_id,
            type="event",
            channel_id=chunk.channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload=payload,
            event_type=event_type,
            metadata=merged_metadata,
            app_id=app_id,
            agent_ref=chunk_agent_ref,
            group_digital_avatar=group_digital_avatar,
            enable_memory=enable_memory,
        )

    @staticmethod
    def _is_terminal_stream_chunk(chunk: AgentResponseChunk) -> bool:
        """识别仅用于结束流的哨兵 chunk，避免被当作业务事件继续下发。"""
        if not bool(getattr(chunk, "is_complete", False)):
            return False
        payload = getattr(chunk, "payload", None)
        if not payload:
            return True
        if not isinstance(payload, dict):
            return False
        if payload.get("event_type"):
            return False
        if payload.get("content") not in (None, ""):
            return False
        if payload.get("error") not in (None, ""):
            return False
        return payload.get("is_complete") is True and set(payload.keys()) <= {"is_complete"}

    async def publish_stream_chunk(
        self,
        chunk: AgentResponseChunk,
        *,
        session_id: str | None,
        request_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Publish one AgentServer stream chunk (evolution + robot_messages).

        Returns False when the chunk should not be forwarded to robot_messages
        (terminal sentinel or filtered control message like keepalive).
        """
        if self._is_terminal_stream_chunk(chunk):
            return False
        # 跳过 keepalive chunk — 仅用于 WebSocket 保活，不投递到 IM 通道
        payload = getattr(chunk, "payload", None)
        if isinstance(payload, dict) and payload.get("event_type") == "keepalive":
            return False
        if not await self._handle_evolution_chunk(chunk, session_id, request_metadata):
            return False
        out = self._chunk_to_message(
            chunk,
            session_id=session_id,
            metadata=request_metadata,
            app_id=self._stream_app_ids.get(chunk.request_id, ""),
        )
        await self.publish_robot_messages(out)
        return True

    async def _publish_stream_cancelled_final(
        self,
        request_id: str,
        channel_id: str,
        session_id: str | None,
        request_metadata: dict[str, Any] | None,
    ) -> None:
        """流式任务被网关取消时补发 chat.final，带 is_complete（供飞书等通道合并缓冲）。"""
        from jiuwenswarm.common.schema.message import Message, EventType

        group_digital_avatar = bool(request_metadata.get("group_digital_avatar", False)) if request_metadata else False
        enable_memory = bool(request_metadata.get("enable_memory", True)) if request_metadata else True

        out = Message(
            id=request_id,
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": EventType.CHAT_FINAL.value,
                "content": "",
                "is_complete": True,
            },
            event_type=EventType.CHAT_FINAL,
            metadata=request_metadata,
            group_digital_avatar=group_digital_avatar,
            enable_memory=enable_memory,
        )
        await self.publish_robot_messages(out)
        logger.info(
            "[MessageHandler] 已发送流式取消结束帧: request_id=%s session_id=%s",
            request_id,
            session_id,
        )

    async def _publish_stream_connection_error(
        self,
        request_id: str,
        channel_id: str,
        session_id: str | None,
        request_metadata: dict[str, Any] | None,
        error: str,
    ) -> None:
        """Publish a visible stream error when the AgentServer connection drops."""
        from jiuwenswarm.common.schema.message import Message, EventType

        out = Message(
            id=request_id,
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=False,
            payload={
                "event_type": EventType.CHAT_ERROR.value,
                "error": error,
                "code": "AGENT_SERVER_CONNECTION_CLOSED",
                "is_complete": True,
            },
            event_type=EventType.CHAT_ERROR,
            metadata=request_metadata,
        )
        await self.publish_robot_messages(out)
        logger.warning(
            "[MessageHandler] Stream 因 AgentServer WebSocket 断开而结束: request_id=%s error=%s",
            request_id,
            error,
        )

    @staticmethod
    def _non_stream_rpc_may_run_parallel(env: "E2AEnvelope") -> bool:
        """可与其它非流式 RPC 并发，不阻塞 _forward_loop。

        网关队列否则串行 await Agent，慢请求（如 SkillNet 搜索）会堵住后续的 skills.list 刷新。
        聊天相关必须按入队顺序与流式任务协调，不得后台并发。
        """
        from jiuwenswarm.common.schema.message import ReqMethod

        m = getattr(env.method, "value", env.method)
        if not m:
            return False
        return m not in (
            ReqMethod.CHAT_SEND.value,
            ReqMethod.CHAT_RESUME.value,
            ReqMethod.CHAT_CANCEL.value,
            ReqMethod.CHAT_ANSWER.value,
        )

    @staticmethod
    def _should_trigger_before_chat_request_hook(msg: "Message") -> bool:
        from jiuwenswarm.common.schema.message import ReqMethod

        return msg.req_method in (
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        )

    @staticmethod
    def _should_emit_processing_status_for_stream(msg: "Message") -> bool:
        from jiuwenswarm.common.schema.message import ReqMethod

        return msg.req_method == ReqMethod.CHAT_SEND

    def _session_has_streams_blocking_processing_false(
        self, session_id: str | None
    ) -> bool:
        """同 session 是否还有应挡住 ``is_processing=false`` 补发的流.

        - ``chat.send``（emit=True）：正常对话仍在跑
        - ``command.goal``（emit=False 长流）：权限同意短流结束时不得误清前端，
          否则会出现「前端已结束、后端 Goal 还在跑、后续 delta 收不到」

        ``history.get`` 等短只读流不计入，否则 chat 结束时若并发拉历史，会永远
        等不到 false（这些流自身 emit=False，退出时也不会补发）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod

        for active_rid, sid in self._stream_sessions.items():
            if sid != session_id:
                continue
            if self._stream_emits_processing_status.get(active_rid, True):
                return True
            if self._stream_methods.get(active_rid) == ReqMethod.COMMAND_GOAL.value:
                return True
        return False

    @staticmethod
    def _stream_method_value(msg: "Message") -> str:
        """Normalize ``msg.req_method`` to its wire value string."""
        req_method = getattr(msg, "req_method", None)
        if req_method is None:
            return ""
        return str(getattr(req_method, "value", req_method) or "")

    async def _trigger_before_chat_request_hook(self, msg: "Message") -> None:
        if not self._should_trigger_before_chat_request_hook(msg):
            return

        params = msg.params if isinstance(msg.params, dict) else {}
        if not isinstance(msg.params, dict):
            msg.params = params

        ctx = GatewayChatHookContext(
            request_id=msg.id,
            channel_id=msg.channel_id,
            session_id=msg.session_id,
            req_method=msg.req_method.value if msg.req_method is not None else None,
            params=params,
        )
        from jiuwenswarm.extensions.registry import ExtensionRegistry

        await ExtensionRegistry.get_instance().trigger(GatewayHookEvents.BEFORE_CHAT_REQUEST, ctx)

    @staticmethod
    def _is_evolution_approval_request_id(request_id: Any) -> bool:
        return is_evolution_approval_request_id(request_id)

    @staticmethod
    def _is_evolution_approval_payload(payload: Any) -> bool:
        return is_evolution_approval_payload(payload)

    def _is_current_pending_evolution_approval(
        self,
        session_id: str | None,
        request_id: Any,
    ) -> bool:
        return self._evolution_approval.is_current_pending(session_id, request_id)

    def _is_interrupt_evolution_approval_chat_send(
        self,
        msg: "Message",
        *,
        method: Any | None = None,
    ) -> bool:
        effective_method = getattr(method, "value", method)
        if effective_method is None:
            if not self._is_chat_send_message(msg):
                return False
        elif effective_method != "chat.send":
            return False
        if not isinstance(msg.params, dict):
            return False
        return self._is_interrupt_evolution_approval_answer_payload(msg.params)

    @staticmethod
    def _is_interrupt_evolution_approval_answer_payload(payload: Any) -> bool:
        return is_interrupt_evolution_approval_answer_payload(payload)

    async def _dispatch_interrupt_evolution_approval_as_chat_send(
        self,
        msg: "Message",
    ) -> None:
        from jiuwenswarm.common.schema.message import ReqMethod

        if self._is_current_pending_evolution_approval(
            msg.session_id,
            (msg.params or {}).get("request_id") if isinstance(msg.params, dict) else None,
        ):
            self._user_messages.put_nowait(
                replace(msg, req_method=ReqMethod.CHAT_SEND, is_stream=True)
            )
            return
        logger.info(
            "[MessageHandler] stale interrupt evolution approval answer ignored: "
            "session_id=%s request_id=%s",
            msg.session_id,
            (msg.params or {}).get("request_id") if isinstance(msg.params, dict) else None,
        )

    @staticmethod
    def _ensure_regular_evolution_approval_metadata(payload: dict[str, Any]) -> dict[str, Any]:
        return ensure_regular_evolution_approval_metadata(payload)

    async def _complete_evolution_approval_if_current(
        self,
        msg: "Message",
        answered_request_id: str | None,
    ) -> None:
        finish_result = self._evolution_approval.finish_if_current(
            msg.session_id,
            answered_request_id,
        )
        if finish_result is None:
            return

        promoted_approval = finish_result.promoted_approval
        if promoted_approval is not None:
            promoted_chunk = SimpleNamespace(
                request_id=promoted_approval.chunk_request_id,
                channel_id=promoted_approval.channel_id,
                payload=promoted_approval.payload,
            )
            out = self._chunk_to_message(
                promoted_chunk,
                session_id=msg.session_id,
                metadata=promoted_approval.metadata,
                app_id=msg.app_id or "",
            )
            await self.publish_robot_messages(out)
            logger.info(
                "[MessageHandler] evolution approval answered (resolved), "
                "deferred approval published: request_id=%s session_id=%s",
                promoted_approval.request_id,
                msg.session_id,
            )
            await self._send_processing_status(
                msg.id,
                msg.session_id,
                msg.channel_id,
                is_processing=False,
                app_id=msg.app_id or "",
            )
            return

        queued_payload = finish_result.queued_supplement
        queued_input = str((queued_payload or {}).get("new_input") or "").strip()
        queued_attachments = (queued_payload or {}).get("attachments")
        if queued_input:
            queued_msg = self._build_queued_chat_send_message(
                msg,
                queued_input,
                queued_attachments if isinstance(queued_attachments, list) else None,
                self._get_session_last_user_query(msg.session_id),
            )
            self._user_messages.put_nowait(queued_msg)
            logger.info(
                "[MessageHandler] evolution approval answered (resolved), "
                "queued supplement dispatched: id=%s session_id=%s",
                queued_msg.id,
                msg.session_id,
            )
        else:
            logger.info(
                "[MessageHandler] evolution approval answered (resolved), "
                "no queued supplement: request_id=%s session_id=%s",
                answered_request_id,
                msg.session_id,
            )
        await self._send_processing_status(
            msg.id,
            msg.session_id,
            msg.channel_id,
            is_processing=False,
            app_id=msg.app_id or "",
        )

    @staticmethod
    def _approval_response_resolved(resp: Any) -> bool:
        return (
            resp is not None
            and hasattr(resp, "payload")
            and isinstance(resp.payload, dict)
            and resp.payload.get("resolved", False) is True
        )

    async def _handle_evolution_chunk(
        self,
        chunk,
        session_id: str | None,
        request_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """处理 chunk 中的演进状态和审批事件，更新 Gateway 状态机。

        在 process_stream 和 _handle_agent_server_push 两条路径中复用。
        返回 False 表示该 chunk 已被延后处理，不应继续发布给前端。
        """
        payload = getattr(chunk, "payload", None)
        auto_save_enabled = (
            self._evolution_auto_save_enabled
            if (
                isinstance(payload, dict)
                and payload.get("event_type") == "chat.ask_user_question"
                and self._is_evolution_approval_payload(payload)
                and not self._is_interrupt_evolution_approval_answer_payload(payload)
            )
            else False
        )
        decision = self._evolution_approval.handle_chunk(
            chunk,
            session_id,
            request_metadata,
            auto_save_enabled=auto_save_enabled,
        )
        if decision.user_message is not None:
            self._user_messages.put_nowait(decision.user_message)
        return decision.should_publish_chunk

    def _clear_session_evolution_states(self, session_id: str | None) -> None:
        self._evolution_approval.clear_session(session_id)

    @staticmethod
    def _build_supplement_continuation_query(
        new_input: str,
        original_request: str = "",
    ) -> str:
        trimmed = str(new_input or "").strip()
        original = str(original_request or "").strip()
        original_section = (
            f"\n\n原始任务请求如下，请以它作为继续执行 todo 时的上下文，尤其要保留其中的文件路径、目录、约束和目标：\n{original[:8000]}"
            if original
            else ""
        )
        return (
            "用户在当前任务执行中追加了补充/调整请求：\n"
            f"{trimmed}\n\n"
            "请先处理这个补充/调整请求，然后检查并继续执行当前会话 todo 列表中仍未完成的 "
            "in_progress 或 pending 任务。不要因为补充请求本身处理完成就询问用户下一步；"
            "只有在确认 todo 列表没有未完成任务时，才可以总结或询问后续方向。\n\n"
            "注意：追加补充请求会中断上一轮流式输出，用户界面上上一轮正在输出的任务结果可能只展示了一部分。"
            "如果补充请求发生时某个 todo 正在输出结果，或者 todo 状态已经前进但该任务结果可能没有完整展示，"
            "继续执行时请先补全或简要重述这个被中断任务的完整结果，再推进后续 todo；"
            "不要仅因为 todo 状态已经变为 completed 就跳过用户尚未完整看到的任务结果。"
            f"{original_section}"
        )

    @staticmethod
    def _build_queued_chat_send_message(
        msg: "Message",
        new_input: str,
        attachments: list[dict[str, Any]] | None = None,
        original_request: str = "",
    ) -> "Message":
        from jiuwenswarm.common.schema.message import Message, ReqMethod

        new_req_id = f"req_{int(time.time() * 1000):x}_{msg.id}"
        params: dict[str, Any] = {
            "query": MessageHandler._build_supplement_continuation_query(
                new_input,
                original_request,
            ),
            "supplement_input": new_input,
            "original_request": original_request,
            "session_id": msg.session_id,
            "is_supplement": True,
        }
        if attachments:
            params["attachments"] = attachments
        return Message(
            id=new_req_id,
            type="req",
            channel_id=msg.channel_id,
            session_id=msg.session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            user_id=getattr(msg, "user_id", None),
        )

    async def _send_non_stream_agent_request(
        self,
        env: "E2AEnvelope",
    ) -> "AgentResponse":
        return await send_agent_request_with_timeout(
            self.agent_client,
            env,
            label="MessageHandler",
        )

    async def _process_non_stream_request(self, msg: "Message", env: "E2AEnvelope") -> Any:
        """执行单次非流式 Agent 请求并将结果写入 robot_messages（供串行或后台任务复用）。"""
        try:
            resp = await self._send_non_stream_agent_request(env)
            out = self._response_to_message(
                resp,
                session_id=msg.session_id,
                request_metadata=msg.metadata,
                app_id=msg.app_id or "",
            )
            await self.publish_robot_messages(out)
            logger.info(
                "[MessageHandler] Agent 响应已写入 robot_messages: request_id=%s channel_id=%s",
                resp.request_id,
                resp.channel_id,
            )
            if (
                self._is_interrupt_evolution_approval_chat_send(msg, method=env.method)
                and self._approval_response_resolved(resp)
            ):
                answer_payload = msg.params if isinstance(msg.params, dict) else {}
                answer_request_id = answer_payload.get("request_id")
                await self._complete_evolution_approval_if_current(
                    msg,
                    str(answer_request_id or ""),
                )
            return resp
        except Exception as e:
            logger.exception("AgentServer send_request failed for %s: %s", msg.id, e)
            err_msg = self._build_error_out_message(msg, e)
            await self.publish_robot_messages(err_msg)
            logger.info(
                "[MessageHandler] 错误响应已写入 robot_messages: id=%s channel_id=%s",
                msg.id,
                msg.channel_id,
            )
            return None
        finally:
            # A non-stream AgentServer call can still emit cron server_push
            # frames while it is awaited.  Release its owner mapping only after
            # the request completes.
            self._stream_user_ids.pop(env.request_id or "", None)

    # ---------- 入队 -> AgentServer -> 出队 转发循环 ----------

    async def _forward_loop(self) -> None:
        """循环：从 user_messages 取消息，经 AgentServerClient 发往 AgentServer，将响应写入 robot_messages.
        支持流式和非流式两种模式。使用 timeout=None 阻塞等待，保证有消息时第一时间被唤醒处理；
        stop 时 task 被 cancel 会打断 get() 并退出。

        支持中断机制：当收到 CHAT_CANCEL 请求时，会立即取消正在执行的流式任务。
        """
        from jiuwenswarm.common.schema.message import ReqMethod

        while self._running:
            try:
                msg = await self.consume_user_messages(timeout=None)
                if msg is None:
                    continue
                
         
                # 先处理受控通道的 Channel 控制指令（如 /new_session、/mode、/skills list）
                if await self._handle_channel_control(msg):
                    # 该消息仅用于修改 session/mode，已给 Channel 回复提示，不再转发给 Agent
                    continue

                # 将当前 Channel 的控制状态应用到消息上
                await self._resolve_external_channel_session(msg)
                self._apply_channel_state(msg)
                channel_type = self._resolve_control_channel_type(msg)
                if (
                    channel_type in self._control_channel_types
                    and not str(msg.session_id or "").strip()
                ):
                    state = self.get_or_create_channel_state(msg)
                    msg.session_id = await self._allocate_channel_session(msg, state)

                # mode is only known after _apply_channel_state, so the team /
                # non-streaming combination is refused here — before GodView
                # registration, which would otherwise subscribe for a round
                # that never runs.
                if self._is_unsupported_non_stream_team_send(msg):
                    await self._reject_non_stream_team_send(msg)
                    continue

                # V2: _apply_channel_state has resolved msg.session_id to the real team
                # session_id and injected params.mode; register GodView now so it lands
                # under the same session team-event dispatch uses.
                await self._maybe_register_godview(msg)

                # Gateway hook: UserPromptSubmit
                if self._gateway_hook_handler:
                    try:
                        params = msg.params if isinstance(msg.params, dict) else {}
                        prompt_text = str(params.get("query") or params.get("content") or "")
                        await self._gateway_hook_handler.on_user_prompt_submit(
                            msg.session_id or "",
                            prompt_text,
                        )
                    except Exception:
                        logger.debug("Gateway hook UserPromptSubmit failed", exc_info=True)


                if msg.req_method == ReqMethod.CHAT_ANSWER:
                    answer_payload = msg.params if isinstance(msg.params, dict) else {}
                    is_evolution_approval_answer = self._is_evolution_approval_payload(answer_payload)
                    if is_evolution_approval_answer:
                        msg.params = self._ensure_regular_evolution_approval_metadata(answer_payload)
                        if self._is_interrupt_evolution_approval_answer_payload(msg.params):
                            await self._dispatch_interrupt_evolution_approval_as_chat_send(msg)
                            continue
                    agent_msg = await self._prepare_agent_dispatch_message(msg)
                    env = self.message_to_e2a(agent_msg)
                    resp = await self._process_non_stream_request(msg, env)
                    answer_payload = msg.params if isinstance(msg.params, dict) else {}
                    answer_request_id = answer_payload.get("request_id")
                    if is_evolution_approval_answer:
                        if self._approval_response_resolved(resp):
                            await self._complete_evolution_approval_if_current(
                                msg,
                                str(answer_request_id or ""),
                            )
                        else:
                            logger.info(
                                "[MessageHandler] evolution approval answered but not resolved: "
                                "id=%s session_id=%s request_id=%s",
                                msg.id,
                                msg.session_id,
                                answer_request_id,
                            )
                    continue

                if msg.req_method == ReqMethod.CHAT_SWARMFLOW_REPLY:
                    # Forward a swarmflow human reply to the agent adapter
                    # (non-stream). Unlike CHAT_ANSWER, no evolution-approval
                    # machinery; unlike CHAT_SEND, no cancel-existing-stream.
                    agent_msg = await self._prepare_agent_dispatch_message(msg)
                    env = self.message_to_e2a(agent_msg)
                    await self._process_non_stream_request(msg, env)
                    continue

                if msg.req_method == ReqMethod.CHAT_CANCEL:
                    logger.info(
                        "[MessageHandler] 收到中断请求: id=%s channel_id=%s",
                        msg.id, msg.channel_id,
                    )
                    new_input = (msg.params or {}).get("new_input")
                    has_new_input = isinstance(new_input, str) and new_input.strip()
                    raw_attachments = (msg.params or {}).get("attachments")
                    supplement_attachments = (
                        raw_attachments if isinstance(raw_attachments, list) else None
                    )
                    intent = (msg.params or {}).get("intent", "cancel")

                    if has_new_input:
                        if self._evolution_approval.should_queue_supplement(msg.session_id):
                            queued_input = new_input.strip()
                            self._evolution_approval.queue_supplement(
                                msg.session_id,
                                queued_input,
                                supplement_attachments,
                            )
                            logger.info(
                                "[MessageHandler] evolution phase pending, queue supplement input: session_id=%s",
                                msg.session_id,
                            )
                            await self._send_interrupt_result_notification(
                                msg.id,
                                msg.channel_id,
                                msg.session_id,
                                "supplement",
                                message="已加入队列，等待演进完成",
                            )
                            continue


                        # 有新输入：取消旧任务 → 保留 todo → 启动新任务（非并发）

                        # 1. 取消 gateway 侧当前 session 相关的流式任务（而非所有任务）
                        tasks_to_cancel = []
                        rids_cancelled = []
                        current_sid = msg.session_id
                        for rid, task in list(self._stream_tasks.items()):
                            # 只取消与当前 session_id 关联的任务
                            if self._stream_sessions.get(rid) != current_sid:
                                continue
                            if not task.done():
                                logger.info(
                                    "[MessageHandler] supplement: 取消流式任务 request_id=%s session_id=%s",
                                    rid, current_sid,
                                )
                                task.cancel()
                                tasks_to_cancel.append(task)
                                rids_cancelled.append(rid)
                        if tasks_to_cancel:
                            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

                        # 2. 通知前端 supplement（前端据此判断 is_processing 状态）
                        await self._send_interrupt_result_notification(
                            msg.id, msg.channel_id, msg.session_id, "supplement",
                        )

                        # 3. 发送 supplement intent 到 AgentServer（取消任务但保留 todo）
                        #    用 await 确保 agent 侧先完成取消再启动新任务
                        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

                        agent_msg = await self._prepare_agent_dispatch_message(msg)
                        source_params = msg.params if isinstance(msg.params, dict) else {}
                        runtime_params: dict[str, Any] = {}
                        for key in ("cwd", "trusted_dirs", "mode"):
                            value = source_params.get(key)
                            if value:
                                runtime_params[key] = value
                        if "cwd" not in runtime_params and isinstance(msg.metadata, dict):
                            metadata_cwd = msg.metadata.get("cwd")
                            if isinstance(metadata_cwd, str) and metadata_cwd.strip():
                                runtime_params["cwd"] = metadata_cwd.strip()
                        # 注入 mode 信息，确保 AgentServer 找到正确的 agent
                        sup_state = self._channel_states.get(
                            self._get_channel_state_key(msg.channel_id, msg.session_id)
                        ) or self._channel_states.get(msg.channel_id)
                        if "mode" not in runtime_params and sup_state is not None:
                            runtime_params["mode"] = (
                                sup_state.mode.value
                                if hasattr(sup_state.mode, 'value')
                                else str(sup_state.mode)
                            )
                        supplement_params = {
                            "intent": "supplement",
                            "new_input": new_input.strip(),
                            "session_id": agent_msg.session_id,
                            **runtime_params,
                        }
                        supplement_env = e2a_from_agent_fields(
                            request_id=f"supplement_{int(time.time() * 1000):x}",
                            channel_id=msg.channel_id,
                            session_id=agent_msg.session_id,
                            req_method=ReqMethod.CHAT_CANCEL,
                            params=supplement_params,
                            is_stream=False,
                            timestamp=time.time(),
                            metadata=msg.metadata,
                            user_id=getattr(msg, "user_id", None),
                        )
                        try:
                            resp = await self._send_non_stream_agent_request(supplement_env)
                            # 发送被中断工具的 tool_result 给前端
                            payload = resp.payload if isinstance(resp.payload, dict) else {}
                            await self._send_cancelled_tool_results(
                                msg.channel_id, msg.session_id, payload, msg.metadata
                            )
                        except Exception:
                            pass  # 即使失败也继续启动新任务

                        # 4. 入队新任务（单一任务，不并发）
                        from jiuwenswarm.common.schema.message import Message

                        new_req_id = f"req_{int(time.time() * 1000):x}_{msg.id}"
                        sup_meta = dict(msg.metadata) if msg.metadata else None
                        original_request = self._get_session_last_user_query(msg.session_id)
                        new_msg = Message(
                            id=new_req_id,
                            type="req",
                            channel_id=msg.channel_id,
                            session_id=msg.session_id,
                            params={
                                "query": self._build_supplement_continuation_query(
                                    new_input,
                                    original_request,
                                ),
                                "supplement_input": new_input.strip(),
                                "original_request": original_request,
                                "session_id": msg.session_id,
                                "is_supplement": True,
                                **runtime_params,
                                **(
                                    {"model_name": (msg.params or {}).get("model_name")}
                                    if (msg.params or {}).get("model_name")
                                    else {}
                                ),
                                **(
                                    {"attachments": supplement_attachments}
                                    if supplement_attachments
                                    else {}
                                ),
                            },
                            timestamp=time.time(),
                            ok=True,
                            req_method=ReqMethod.CHAT_SEND,
                            is_stream=True,
                            provider=msg.provider,
                            chat_id=msg.chat_id,
                            user_id=msg.user_id,
                            bot_id=msg.bot_id,
                            metadata=sup_meta,
                        )
                        self._user_messages.put_nowait(new_msg)
                        logger.info(
                            "[MessageHandler] supplement: 旧任务已取消，新任务已入队: id=%s session_id=%s",
                            new_msg.id, msg.session_id,
                        )

                    elif intent == "cancel":
                        # fire_and_forget：避免慢 cancel 阻塞 _forward_loop，
                        # 导致后续 session.create 等请求在队列中等待、前端超时。
                        await self._cancel_agent_work_for_session(
                            msg,
                            msg.session_id,
                            agent_notify="fire_and_forget",
                        )

                    elif intent in ("pause", "resume"):
                        # 暂停/恢复：不取消流式任务，转发给 AgentServer 处理 ReAct 循环
                        agent_msg = await self._prepare_agent_dispatch_message(msg)
                        # 确保 mode 信息存在，否则从 channel_states 注入
                        if isinstance(agent_msg.params, dict) and not agent_msg.params.get("mode"):
                            pr_state = self._channel_states.get(
                                self._get_channel_state_key(msg.channel_id, msg.session_id)
                            ) or self._channel_states.get(msg.channel_id)
                            if pr_state is not None:
                                agent_msg.params["mode"] = (
                                    pr_state.mode.value
                                    if hasattr(pr_state.mode, 'value')
                                    else str(pr_state.mode)
                                )
                        env_interrupt = self.message_to_e2a(agent_msg)
                        asyncio.create_task(self._send_interrupt_to_agent(env_interrupt))
                        # 检查当前 session 是否有活跃的流式任务
                        current_sid = msg.session_id
                        has_active_task = False
                        for rid, task in list(self._stream_tasks.items()):
                            if self._stream_sessions.get(rid) == current_sid and not task.done():
                                has_active_task = True
                                break
                        # 通知前端状态变更（事件）
                        await self._send_interrupt_result_notification(
                            msg.id, msg.channel_id, msg.session_id, intent,
                            has_active_task=has_active_task,
                        )

                    continue

                # ---- Inbound Pipeline（数字分身入站过滤）----
                if self._inbound_pipeline is not None and msg.req_method == ReqMethod.CHAT_SEND:
                    try:
                        should_forward = await self._inbound_pipeline.apply(msg)
                    except Exception:
                        logger.exception("Inbound pipeline error, fallback to forwarding")
                    else:
                        if not should_forward:
                            continue  # 不相关消息，跳过

                # ---- Resolve @file references in chat.send content ----
                if msg.req_method == ReqMethod.CHAT_SEND and msg.params:
                    content = msg.params.get("query") or msg.params.get("content") or ""
                    attachments = msg.params.get("attachments")
                    if isinstance(content, str):
                        # ---- Resolve /review and /security-review slash commands (all channels) ----
                        stripped = content.strip()
                        if stripped:
                            parsed = parse_channel_control_text(stripped)
                            if parsed.action is ParsedControlAction.REVIEW_BAD:
                                asyncio.create_task(
                                    self.send_channel_notice(
                                        {"id": msg.id, "meta_data": msg.metadata},
                                        msg.channel_id,
                                        msg.session_id,
                                        "非法指令，/review 参数过长或含有非法控制字符",
                                    )
                                )
                                continue
                            if parsed.action is ParsedControlAction.REVIEW_OK:
                                pr_arg = parsed.pr_arg or ""
                                review_prompt = build_review_prompt(pr_arg)
                                msg.params = dict(msg.params)
                                msg.params["query"] = review_prompt
                                if "content" in msg.params:
                                    msg.params["content"] = review_prompt
                                content = review_prompt
                                logger.info(
                                    "[MessageHandler] /review prompt injected for chat.send "
                                    "channel=%s pr_arg=%s",
                                    getattr(msg, "channel_id", ""),
                                    pr_arg or "<none>",
                                )
                            elif parsed.action is ParsedControlAction.SECURITY_REVIEW_BAD:
                                asyncio.create_task(
                                    self.send_channel_notice(
                                        {"id": msg.id, "meta_data": msg.metadata},
                                        msg.channel_id,
                                        msg.session_id,
                                        "非法指令，/security-review 参数过长或含有非法控制字符",
                                    )
                                )
                                continue
                            elif parsed.action is ParsedControlAction.SECURITY_REVIEW_OK:
                                extra_arg = parsed.security_review_arg or ""
                                cwd = (
                                    msg.metadata.get("cwd")
                                    if isinstance(msg.metadata, dict)
                                    else None
                                )
                                try:
                                    # 预执行只读 git 并内联输出；
                                    # 失败则发 notice 中止，不转发给 Agent。
                                    # 跑在 executor 里，避免 4×30s 同步 subprocess 阻塞转发循环。
                                    security_prompt = (
                                        await asyncio.get_event_loop().run_in_executor(
                                            None,
                                            build_security_review_prompt,
                                            extra_arg,
                                            cwd,
                                        )
                                    )
                                except GitPreExecError as exc:
                                    await self.send_channel_notice(
                                        {"id": msg.id, "meta_data": msg.metadata},
                                        msg.channel_id,
                                        msg.session_id,
                                        f"/security-review 无法执行：{exc}",
                                    )
                                    continue
                                msg.params = dict(msg.params)
                                msg.params["query"] = security_prompt
                                if "content" in msg.params:
                                    msg.params["content"] = security_prompt
                                content = security_prompt
                                logger.info(
                                    "[MessageHandler] /security-review prompt injected "
                                    "for chat.send channel=%s extra_arg=%s",
                                    getattr(msg, "channel_id", ""),
                                    extra_arg or "<none>",
                                )

                        cwd = None
                        if isinstance(msg.metadata, dict):
                            cwd = msg.metadata.get("cwd")
                        enriched = content
                        if attachments:
                            enriched = self._resolve_structured_attachments(
                                content,
                                attachments,
                                cwd=cwd,
                            )
                        elif content and "@" in content:
                            enriched = self.resolve_at_file_references(content, cwd=cwd)

                        # ---- Resolve @agent-xxx mentions ----
                        agent_mentions = self.extract_agent_mentions(content)
                        if agent_mentions:
                            hint_parts = []
                            for agent_name in agent_mentions:
                                hint_parts.append(
                                    f"用户表达了调用智能体 \"{agent_name}\" 的意图。"
                                    f"请按需调用该智能体，并向其传递所需的上下文。"
                                )
                            agent_hint = "\n".join(hint_parts)
                            enriched = (
                                enriched
                                + "\n\n<system-reminder>\n"
                                + agent_hint
                                + "\n</system-reminder>"
                            )
                            logger.info(
                                "[MessageHandler] Agent mentions detected: %s",
                                agent_mentions,
                            )

                        if enriched != content:
                            msg.params = dict(msg.params)
                            msg.params["query"] = enriched
                            if "content" in msg.params:
                                msg.params["content"] = enriched
                            logger.info(
                                "[MessageHandler] attachments/agent-mentions resolved in chat.send: id=%s",
                                msg.id,
                            )

                logger.info(
                    "[MessageHandler] dispatch: request_id=%s channel=%s session_id=%s user_id=%s is_stream=%s",
                    msg.id,
                    msg.channel_id,
                    msg.session_id,
                    getattr(msg, "user_id", "") or "",
                    msg.is_stream,
                    extra={"session_id": msg.session_id} if msg.session_id else {},
                )
                if self._is_interrupt_evolution_approval_chat_send(msg):
                    if self._is_current_pending_evolution_approval(
                        msg.session_id,
                        (msg.params or {}).get("request_id") if isinstance(msg.params, dict) else None,
                    ):
                        msg = replace(msg, is_stream=True)
                    else:
                        logger.info(
                            "[MessageHandler] stale interrupt evolution approval chat.send ignored: "
                            "session_id=%s request_id=%s",
                            msg.session_id,
                            (msg.params or {}).get("request_id") if isinstance(msg.params, dict) else None,
                        )
                        continue
                agent_msg = await self._prepare_agent_dispatch_message(msg)
                await self._trigger_before_chat_request_hook(agent_msg)
                env = self.message_to_e2a(agent_msg)
                stream_rid = env.request_id or msg.id
                # Keep this for both streaming and unary requests: cron tools
                # can emit an asynchronous server_push on either transport.
                self._stream_user_ids[stream_rid] = str(env.user_id or "").strip()
                try:
                    if env.is_stream:
                        # 取消同一 channel 上已有的流式任务，避免会话孤岛
                        # （例如 TUI 发送新消息时，旧 session 仍在后台空跑）
                        if self._should_cancel_existing_stream_before_chat_send(msg):
                            await self._cancel_stream_tasks_for_channel(msg)
                        await self._start_stream_task(msg, env, stream_rid)
                        # 不 await，让流式任务在后台运行，_forward_loop 继续处理下一个消息
                    elif self._non_stream_rpc_may_run_parallel(env):
                        # 非流式且非聊天：后台执行，避免慢 RPC（如 SkillNet）阻塞队列中的其它请求
                        method_label = env.method or "none"
                        asyncio.create_task(
                            self._process_non_stream_request(msg, env),
                            name=f"gw-nonstr-{method_label}-{stream_rid[:24]}",
                        )
                        logger.info(
                            "[MessageHandler] 非流式 RPC 已后台执行: id=%s method=%s",
                            msg.id,
                            method_label,
                        )
                    else:
                        # 非流式 chat.send（如飞书 enable_streaming=False）：同样属于
                        # “用户对话任务在跑”，需让跨窗口配置保存锁感知。流式靠
                        # _send_processing_status 广播；非流式无 processing_status，故在此
                        # 起止点显式写入 _active_chat_tasks 并广播 task.global_running。
                        # value 存 mode，供 has_active_streams() 判断 team 排除用。
                        self._active_chat_tasks[stream_rid] = (
                            msg.params.get("mode", "plan") if isinstance(msg.params, dict) else "plan"
                        )
                        await self._broadcast_task_global_running()
                        try:
                            await self._process_non_stream_request(msg, env)
                        finally:
                            self._active_chat_tasks.pop(stream_rid, None)
                            await self._broadcast_task_global_running()
                except Exception as e:
                    logger.exception("AgentServer send_request failed for %s: %s", msg.id, e)
                    # 流式任务启动在 tracking 写入后、process_stream 成功登记前
                    # 抛异常（典型如 _send_processing_status / create_task 失败）时，
                    # process_stream 自身的 finally 不会执行，残留的
                    # _stream_modes / _stream_emits_processing_status 会让
                    # has_active_streams() 永久误判、前端保存锁永久禁用。
                    # 此处做防御性清理：stream_rid 的 tracking 已写入但任务未登记时清掉。
                    if (
                        stream_rid in self._stream_modes
                        and stream_rid not in self._stream_tasks
                    ):
                        await self._pop_stream_tracking_and_broadcast([stream_rid])
                    err_msg = self._build_error_out_message(msg, e)
                    await self.publish_robot_messages(err_msg)
                    logger.info(
                            "[MessageHandler] 错误响应已写入 robot_messages: id=%s channel_id=%s",
                        msg.id, msg.channel_id,
                    )
            except asyncio.CancelledError:
                break

    async def process_stream(
        self,
        env: "E2AEnvelope",
        session_id: str | None,
        request_metadata: dict[str, Any] | None,
        *,
        emit_processing_status: bool = True,
    ) -> None:
        """处理流式请求，逐个 chunk 写入 robot_messages.

        这个方法被包装为 Task，在后台运行，可以被随时取消。
        遥测可通过替换类上的 ``process_stream`` 进行打点。
        """
        rid = env.request_id or ""
        channel_id = env.channel or ""
        stream_app_id = self._stream_app_ids.get(rid, "")  # 提前捕获，_pop_stream_tracking 后会清除
        cancelled = False
        has_processing_status_false = False
        _proc_count = 0
        logger.info(
            "[MessageHandler] process_stream started: request_id=%s channel=%s session_id=%s",
            rid, channel_id, session_id,
            extra={"session_id": session_id} if session_id else {},
        )
        try:
            await self._sync_agentos_cron_jobs(env)
            async for chunk in self.agent_client.send_request_stream(env):
                _proc_count += 1
                if _proc_count <= 3:
                    _pl = getattr(chunk, "payload", None) or {}
                    _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                    logger.info(
                        "[MessageHandler] process_stream chunk #%s: request_id=%s event_type=%s",
                        _proc_count, rid, _et,
                    )
                if self._is_terminal_stream_chunk(chunk):
                    continue
                published = await self.publish_stream_chunk(
                    chunk,
                    session_id=session_id,
                    request_metadata=request_metadata,
                )
                if not published:
                    continue
                payload = chunk.payload or {}
                if isinstance(payload, dict):
                    event_type = payload.get("event_type")
                    if event_type == "chat.processing_status":
                        if payload.get("is_processing") is False:
                            has_processing_status_false = True
                    elif event_type == "chat.processing_status_deferred":
                        # Internal placeholder from the cluster-mode
                        # follow-up short stream: the real round-complete
                        # signal will be broadcast by the background team
                        # stream on team.completed. This marker only
                        # prevents the Gateway from auto-emitting
                        # is_processing=False when this short stream
                        # ends, and is NOT forwarded to the frontend.
                        has_processing_status_false = True
                        continue

                _has_fanout = bool((getattr(chunk, "metadata", None) or {}).get("fan_out_targets"))
                logger.debug(
                    "[MessageHandler] Stream chunk 已写入 robot_messages:"
                    " request_id=%s event_type=%s has_fanout=%s",
                    chunk.request_id,
                    payload.get("event_type") if isinstance(payload, dict) else None,
                    _has_fanout,
                )
            logger.info(
                "[MessageHandler] Stream 正常完成: request_id=%s total_chunks=%s",
                rid, _proc_count,
            )
        except asyncio.CancelledError:
            cancelled = True
            logger.info(
                "[MessageHandler] Stream 被取消: request_id=%s total_chunks=%s",
                rid, _proc_count,
            )
        except RuntimeError as exc:
            if "AgentServer WebSocket connection closed" not in str(exc):
                raise
            await self._publish_stream_connection_error(
                rid,
                channel_id,
                session_id,
                request_metadata,
                str(exc),
            )
        except Exception as exc:
            logger.exception(
                "[MessageHandler] Stream 异常: request_id=%s total_chunks=%s error=%s",
                rid, _proc_count, exc,
            )
            await self._publish_stream_cancelled_final(
                rid, channel_id, session_id, request_metadata,
            )
            raise  # 重新抛出，让调用者知道任务被取消
        finally:
            if (
                not cancelled
                and self._is_interrupt_evolution_approval_answer_payload(env.params)
            ):
                from jiuwenswarm.common.schema.message import Message, ReqMethod

                await self._complete_evolution_approval_if_current(
                    Message(
                        id=rid,
                        type="req",
                        channel_id=channel_id,
                        session_id=session_id,
                        params=dict(env.params or {}),
                        timestamp=time.time(),
                        ok=True,
                        req_method=ReqMethod.CHAT_SEND,
                        is_stream=True,
                        metadata=request_metadata,
                    ),
                    str((env.params or {}).get("request_id") or ""),
                )
                has_processing_status_false = True
            # 清理状态：pop tracking 后必须广播 task.global_running 最新快照。
            # 若只裸 pop（不广播），非 chat.send 流（history.get / chat.error 等
            # emit=False 路径）退出时会让 has_active_streams() 计数悬空，前端保存锁
            # 卡在最后一次广播值（通常为 True），仅靠重连自愈。
            await self._pop_stream_tracking_and_broadcast([rid])
            if session_id is not None and session_id not in self._stream_sessions.values():
                # Fallback cleanup when stream exits unexpectedly without evolution end signal.
                self._evolution_approval.clear_session_in_progress(session_id)
            # 标注本 rid 退出后是否还会触发 task.global_running 广播。
            # 若以下分支均不进入 _send_processing_status（其内部会广播），
            # 则前端保存锁可能卡在最后一次广播值，仅靠重连自愈。
            # cancelled 也会补发 is_processing=false（见下方），故不再排除 cancelled。
            # Block false only while chat.send or command.goal remains — not
            # arbitrary emit=False streams (e.g. history.get). Otherwise a short
            # permission-resume chat.send would clear is_processing while Goal
            # is still running; but history.get must not strand the spinner.
            session_blocks_processing_false = (
                self._session_has_streams_blocking_processing_false(session_id)
            )
            _will_broadcast = (
                emit_processing_status
                and not has_processing_status_false
                and not session_blocks_processing_false
            )
            logger.info(
                "[task.global_running] stream 退出: rid=%s cancelled=%s emit=%s "
                "has_status_false=%s remaining_stream=%d remaining_chat=%d "
                "will_broadcast_processing_status=%s",
                rid, cancelled, emit_processing_status, has_processing_status_false,
                len(self._stream_modes), len(self._active_chat_tasks),
                _will_broadcast,
            )
            logger.debug(
                "[MessageHandler] Stream 任务状态已清理: request_id=%s",
                rid,
            )
            # 该 session 流式任务结束后，通知前端处理完成（is_processing=false）。
            # 只有当 AgentServer 没有发送过 processing_status=false 时才补发。
            # 注意：这里不再排除 cancelled 分支。stream 被 esc 取消（cancelled=True）时，
            # AgentServer 的 interrupt_result 可能要等很久（如命中 agent 首次初始化的同步阻塞窗口，
            # 事件循环读不出 cancel 消息），此时前端会一直转圈。主动补发一次 is_processing=false，
            # 让 TUI 立刻停转圈，不等 AgentServer 回 interrupt_result。
            # 守卫：同 session 还有 chat.send 或 command.goal 时不误发 false
            # （避免权限同意短流在 Goal 仍跑时清前端；history.get 不挡）。
            if emit_processing_status and not has_processing_status_false:
                if not session_blocks_processing_false:
                    await self._send_processing_status(
                        rid, session_id, channel_id,
                        is_processing=False, app_id=stream_app_id,
                    )
                    logger.info(
                        "[MessageHandler] 该 session 流式任务已结束（cancelled=%s），已发送 is_processing=false: session_id=%s",
                        cancelled,
                        session_id,
                    )

    async def _sync_agentos_cron_jobs(self, env: "E2AEnvelope") -> None:
        """Provide the routed AgentServer an ephemeral view of Gateway cron jobs.

        Gateway remains the sole persistent job-store owner.  This one-way E2A
        request occurs before an Agent turn so cron tools can list/update jobs
        after an AgentServer restart without reading a user-local cron file.
        Failure is non-fatal for ordinary chat; mutation attempts will still
        report their Gateway delivery failure through the existing path.
        """
        from jiuwenswarm.gateway.routing.e2a_proxy import is_agentos_routing_client

        if not is_agentos_routing_client(self.agent_client):
            return
        controller = self._cron_controller
        if controller is None:
            return
        user_id = str(getattr(env, "user_id", "") or "").strip()
        if not user_id:
            logger.warning(
                "[Cron] skip AgentOS cron snapshot without user_id: request_id=%s",
                env.request_id,
            )
            return
        try:
            jobs = await controller.list_jobs()
            user_jobs = [
                job
                for job in jobs
                if str(job.get("user_id") or "").strip() == user_id
            ]
            from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenswarm.common.schema.message import ReqMethod

            snapshot_env = e2a_from_agent_fields(
                request_id=f"cron-jobs-sync-{env.request_id}",
                channel_id=env.channel or "web",
                session_id=env.session_id,
                req_method=ReqMethod.CRON_JOBS_SYNC,
                params={"jobs": user_jobs},
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id,
            )
            response = await send_agent_request_with_timeout(
                self.agent_client,
                snapshot_env,
                label="cron.jobs.sync",
            )
            if not response.ok:
                logger.warning(
                    "[Cron] AgentOS cron snapshot rejected: request_id=%s payload=%s",
                    env.request_id,
                    response.payload,
                )
        except Exception as exc:  # noqa: BLE001 - must not block normal chat
            logger.warning(
                "[Cron] AgentOS cron snapshot sync failed: request_id=%s error=%s",
                env.request_id,
                exc,
            )

    async def _send_stream_cancelled_notification(
        self, request_id: str | None, channel_id: str, session_id: str | None
    ) -> None:
        """发送流式任务被取消的通知到客户端."""
        if not request_id:
            return

        from jiuwenswarm.common.schema.message import Message, EventType

        cancel_msg = Message(
            id=request_id,
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": "chat.interrupt_result",
                "intent": "cancel",
                "success": True,
                "message": "任务已取消",
            },
            event_type=EventType.CHAT_INTERRUPT_RESULT,
            metadata=None,
        )
        await self.publish_robot_messages(cancel_msg)
        logger.info(
            "[MessageHandler] 已发送流式任务取消通知: request_id=%s",
            request_id,
        )

    async def _start_stream_task(
        self,
        msg: "Message",
        env: "E2AEnvelope",
        stream_rid: str,
    ) -> None:
        """Start a Gateway stream consumer and register stream bookkeeping."""
        emit_processing_status = self._should_emit_processing_status_for_stream(msg)
        self._stream_emits_processing_status[stream_rid] = emit_processing_status
        self._stream_methods[stream_rid] = self._stream_method_value(msg)
        self._stream_modes[stream_rid] = (
            msg.params.get("mode", "plan") if isinstance(msg.params, dict) else "plan"
        )
        if emit_processing_status:
            await self._send_processing_status(
                stream_rid,
                msg.session_id,
                msg.channel_id,
                is_processing=True,
                app_id=msg.app_id or "",
            )
        task = asyncio.create_task(
            self.process_stream(
                env,
                msg.session_id,
                msg.metadata,
                emit_processing_status=emit_processing_status,
            )
        )
        self._stream_tasks[stream_rid] = task
        self._stream_channels[stream_rid] = msg.channel_id
        self._stream_sessions[stream_rid] = msg.session_id
        self._stream_metadata[stream_rid] = msg.metadata
        self._stream_user_ids[stream_rid] = str(env.user_id or "").strip()
        self._stream_app_ids[stream_rid] = msg.app_id or ""
        logger.info(
            "[MessageHandler] Stream 任务已启动（后台运行）: request_id=%s "
            "channel_id=%s 当前并发=%d",
            stream_rid, msg.channel_id, len(self._stream_tasks),
        )

    async def _send_interrupt_to_agent(
        self,
        env: "E2AEnvelope",
        *,
        channel_id: str | None = None,
        session_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Fire-and-forget: 发送中断到 AgentServer，不阻塞转发循环.

        ``interrupt_result`` 已由调用方提前推送；此处仍要把响应里的
        ``cancelled_tools`` 转成 ``chat.tool_result``，否则前端 tool 卡片会一直转圈，
        直到刷新历史才看到 ``[Interrupted]``.
        """
        try:
            resp = await self._send_non_stream_agent_request(env)
            logger.info(
                "[MessageHandler] AgentServer 中断响应: request_id=%s ok=%s",
                resp.request_id, resp.ok,
            )
            payload = resp.payload if isinstance(resp.payload, dict) else {}
            ch = (channel_id or getattr(env, "channel", None) or "").strip()
            sid = (session_id or getattr(env, "session_id", None) or "").strip()
            if ch and sid and payload.get("cancelled_tools"):
                await self._send_cancelled_tool_results(ch, sid, payload, metadata)
        except Exception as e:
            logger.warning("[MessageHandler] AgentServer 中断请求失败(忽略): %s", e)

    async def _send_interrupt_result_notification(
        self,
        request_id: str,
        channel_id: str,
        session_id: str | None,
        intent: str,
        message: str | None = None,
        success: bool = True,
        has_active_task: bool | None = None,
    ) -> None:
        """发送 interrupt_result 事件到前端（pause / resume 等）."""
        from jiuwenswarm.common.schema.message import Message, EventType

        # 根据 has_active_task 调整 pause/resume 的消息
        success_messages_map = {
            "pause": "任务已暂停" if has_active_task else "任务已完成",
            "resume": "任务已恢复" if has_active_task else "任务已完成",
            "cancel": "任务已取消",
            "supplement": "任务已切换",
        }
        failure_messages_map = {
            "pause": "任务暂停失败",
            "resume": "任务恢复失败",
            "cancel": "任务终止失败",
            "supplement": "任务切换失败",
        }
        payload_dict: dict = {
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": success,
            "message": message
            or (
                success_messages_map.get(intent, "任务已中断")
                if success
                else failure_messages_map.get(intent, "任务中断失败")
            ),
        }
        if has_active_task is not None:
            payload_dict["has_active_task"] = has_active_task
        notify_msg = Message(
            id=request_id,
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload=payload_dict,
            event_type=EventType.CHAT_INTERRUPT_RESULT,
            metadata=None,
        )
        await self.publish_robot_messages(notify_msg)
        logger.info(
            "[MessageHandler] 已发送 interrupt_result 通知: intent=%s request_id=%s",
            intent, request_id,
        )

    async def _send_processing_status(
        self, request_id: str, session_id: str | None, channel_id: str, *,
        is_processing: bool, app_id: str = "",
    ) -> None:
        """发送 chat.processing_status 事件到客户端。"""
        from jiuwenswarm.common.schema.message import Message, EventType

        _mode = self._stream_modes.get(request_id)
        status_msg = Message(
            id=request_id,
            type="event",
            channel_id=channel_id,
            app_id=app_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": "chat.processing_status",
                "session_id": session_id,
                "is_processing": is_processing,
                "is_complete": not is_processing
            },
            event_type=EventType.CHAT_PROCESSING_STATUS,
            metadata={"mode": _mode} if _mode else None,
        )
        await self.publish_robot_messages(status_msg)
        # 广播全局运行态快照给所有 ws 客户端（不按 session 路由），用于多窗口配置保存锁。
        # running 取当前活跃任务集合快照（任务结束时该 dict 已 pop 对应 rid），
        # 而非本函数的 is_processing 参数，保证多任务并发时计数准确、幂等。
        await self._broadcast_task_global_running()
        logger.info(
            "[MessageHandler] processing status sent: request_id=%s session_id=%s is_processing=%s",
            request_id,
            session_id,
            is_processing,
        )

    def _build_error_out_message(self, msg: "Message", error: Exception) -> "Message":
        from jiuwenswarm.common.schema.message import Message

        payload: dict[str, Any] = {"error": str(error)}
        code = getattr(error, "code", None)
        if isinstance(code, str) and code:
            payload["code"] = code

        return Message(
            id=msg.id,
            type="res",
            channel_id=msg.channel_id,
            session_id=msg.session_id,
            params={},
            timestamp=time.time(),
            ok=False,
            payload=payload,
            metadata=msg.metadata,
            app_id=msg.app_id or "",
        )

    def _build_tool_result_message(
        self,
        channel_id: str,
        session_id: str,
        tool_info: dict,
        metadata: dict | None,
    ) -> "Message":
        """Build tool_result message for cancelled tool execution."""
        from jiuwenswarm.common.schema.message import Message, EventType

        return Message(
            id=f"tool_result_{int(time.time() * 1000):x}_{secrets.token_hex(3)}",
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "tool_result": {
                    "tool_name": tool_info.get("tool_name", ""),
                    "tool_call_id": tool_info.get("tool_call_id", ""),
                    "result": tool_info.get("result", ""),
                    "status": tool_info.get("status", "error"),
                },
            },
            event_type=EventType.CHAT_TOOL_RESULT,
            metadata=metadata,
        )

    async def _send_cancelled_tool_results(
        self,
        channel_id: str,
        session_id: str,
        payload: dict,
        metadata: dict | None,
    ) -> None:
        """Send cancelled tool results to frontend from interrupt response payload.

        Args:
            channel_id: Channel ID for the message.
            session_id: Session ID for the message.
            payload: Response payload containing cancelled_tools.
            metadata: Message metadata.
        """
        cancelled_tools = payload.get("cancelled_tools", [])
        for tool_info in cancelled_tools:
            await self.publish_robot_messages(
                self._build_tool_result_message(
                    channel_id, session_id, tool_info, metadata
                )
            )

    async def start_forwarding(self) -> None:
        """启动入队 -> AgentServer -> 出队 的转发任务."""
        if self._forward_task is not None:
            return
        self._running = True
        self._forward_task = asyncio.create_task(self._forward_loop())
        logger.info("[MessageHandler] 转发循环已启动 (_user_messages -> AgentServer -> _robot_messages)")

    async def stop_forwarding(self) -> None:
        """停止转发任务."""
        self._running = False

        # 取消所有流式任务
        for rid, task in list(self._stream_tasks.items()):
            if not task.done():
                logger.info("[MessageHandler] 停止时取消流式任务: request_id=%s", rid)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._stream_tasks.clear()
        self._stream_channels.clear()
        self._stream_sessions.clear()
        self._stream_metadata.clear()
        self._stream_user_ids.clear()
        self._stream_emits_processing_status.clear()
        self._stream_methods.clear()
        self._stream_modes.clear()
        pending_disconnect_cancels = list(self._disconnect_cancel_tasks.values())
        for task in pending_disconnect_cancels:
            if not task.done():
                task.cancel()
        if pending_disconnect_cancels:
            await asyncio.gather(*pending_disconnect_cancels, return_exceptions=True)
        self._disconnect_cancel_tasks.clear()
        self._evolution_approval.clear_all()
        self._session_last_user_query.clear()

        # 取消转发循环
        if self._forward_task is not None:
            self._forward_task.cancel()
            try:
                await self._forward_task
            except asyncio.CancelledError:
                pass
            self._forward_task = None

        logger.info("[MessageHandler] 转发循环已停止")

    # ---------- 状态 ----------

    @property
    def user_messages_size(self) -> int:
        return self._user_messages.qsize()

    @property
    def robot_messages_size(self) -> int:
        return self._robot_messages.qsize()
