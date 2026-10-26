# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""路由核心值对象：5 维路由键、Agent 标识、Channel 索引、物理投递地址."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WILDCARD = "*"
APP_ID_DEFAULT = "default"


@dataclass(frozen=True)
class AgentRef:
    """后端智能体标识。

    mode: 'agent' | 'code' | 'team'
    id:   mode='agent'/'code' 时为 agent_id；mode='team' 时为 team_name
    """

    mode: str
    id: str

    @classmethod
    def default(cls) -> "AgentRef":
        return cls(mode="agent", id="default")

    def __repr__(self) -> str:
        return f"AgentRef({self.mode}:{self.id})"


@dataclass(frozen=True)
class RoutingKey:
    """5 维路由键：(user_id, channel_id, app_id, agent_ref, session_id)。

    不可变值对象。通配符 '*' 为监管等高级场景预留，主干路径全用精确值。
    """

    user_id: str
    channel_id: str
    app_id: str
    agent_ref: AgentRef
    session_id: str

    @property
    def channel_key(self) -> "ChannelKey":
        return ChannelKey(self.channel_id, self.app_id)

    @property
    def identity(self) -> "IdentityKey":
        return IdentityKey(self.channel_id, self.app_id, self.user_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "channel_id": self.channel_id,
            "app_id": self.app_id,
            "agent_ref": {"mode": self.agent_ref.mode, "id": self.agent_ref.id},
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class ChannelKey:
    """渠道实例标识，用于 ChannelManager._channels 索引."""

    channel_id: str  # 'feishu' | 'web' | 'wecom' | ...
    app_id: str      # 'cli_xxx' | 'default' | ...

    def __repr__(self) -> str:
        return f"{self.channel_id}:{self.app_id}"


@dataclass(frozen=True)
class IdentityKey:
    """用户身份三元组，用于 SessionSharingRegistry.resolve_member_by_user() 反查."""

    channel_id: str
    app_id: str
    user_id: str


# ── DeliveryTarget 基类 ──────────────────────────────────────


@dataclass(frozen=True)
class DeliveryTarget:
    """物理投递地址基类。

    每渠道独立子类，字段按渠道私有定义。
    Channel 的 send() 通过 isinstance() 判断具体子类后读取专属字段。
    """

    channel_id: str

    @property
    def container_kind(self) -> str:
        raise NotImplementedError

    def get_container_id(self) -> str:
        """物理容器去重标识，用于同容器合并投递."""
        raise NotImplementedError


# ── ws 类 DeliveryTarget ──


@dataclass(frozen=True)
class WebDeliveryTarget(DeliveryTarget):
    """Web ws 投递地址."""
    channel_id: str = "web"
    ws_id: str = ""

    @property
    def container_kind(self) -> str:
        return "ws"

    def get_container_id(self) -> str:
        return self.ws_id


@dataclass(frozen=True)
class TuiDeliveryTarget(DeliveryTarget):
    """TUI 终端投递地址."""
    channel_id: str = "tui"
    ws_id: str = ""

    @property
    def container_kind(self) -> str:
        return "cli"

    def get_container_id(self) -> str:
        return self.ws_id


@dataclass(frozen=True)
class AcpDeliveryTarget(DeliveryTarget):
    """ACP 协议投递地址."""
    channel_id: str = "acp"
    request_id: str = ""

    @property
    def container_kind(self) -> str:
        return "cli"

    def get_container_id(self) -> str:
        return self.request_id


@dataclass(frozen=True)
class XiaoyiDeliveryTarget(DeliveryTarget):
    """小艺 ws/push 双通道投递地址.

    字段对应小艺 message/stream 真实消息样本：
    - agent_id          ← 消息 agentId（per-user agent 标识，= RoutingKey.user_id，去重主键）
    - push_id           ← systemVariables.push_id（webhook 推送 token，随请求携带）
    - xiaoyi_session_id ← 顶层 sessionId（物理回发，临时，ws 投递用）
    - conversation_id   ← conversationId / params.sessionId（逻辑会话，跨请求稳定）
    - url_key           ← ws 连接键（空=双通道都发）
    """
    channel_id: str = "xiaoyi"
    agent_id: str = ""
    push_id: str = ""
    xiaoyi_session_id: str = ""
    conversation_id: str = ""
    url_key: str = ""

    @property
    def container_kind(self) -> str:
        return "ws"

    def get_container_id(self) -> str:
        # 按 agent_id 去重：per-user 稳定（push_id 随请求携带可能为空，不作主键）
        return self.agent_id or self.push_id


# ── IM 类 DeliveryTarget ──


@dataclass(frozen=True)
class FeishuDeliveryTarget(DeliveryTarget):
    """飞书 / 飞书企业版 投递地址（按 channel_id 区分）."""
    channel_id: str = "feishu"             # 'feishu' | 'feishu_enterprise'
    chat_type: str = "group"               # 'group' | 'p2p'
    chat_id: str = ""                      # 群: oc_xxx；私聊: oc_p2p_xxx
    receive_id: str = ""                   # SDK receive_id
    id_type: str = "chat_id"               # 'chat_id' | 'open_id'
    physical_user_id: str = ""             # 飞书 open_id（@ 标签注入用）

    @property
    def container_kind(self) -> str:
        return self.chat_type

    def get_container_id(self) -> str:
        return self.chat_id or self.receive_id


@dataclass(frozen=True)
class WecomDeliveryTarget(DeliveryTarget):
    """企业微信投递地址."""
    channel_id: str = "wecom"
    chat_type: str = "group"               # 'group' | 'p2p'
    chat_id: str = ""                      # 群 chat_id（appchat）
    user_id: str = ""                      # p2p 投递 user_id
    physical_user_id: str = ""             # 企微 userid（@ 标签注入用）

    @property
    def container_kind(self) -> str:
        return self.chat_type

    def get_container_id(self) -> str:
        return self.chat_id or self.user_id


@dataclass(frozen=True)
class DingTalkDeliveryTarget(DeliveryTarget):
    """钉钉投递地址."""
    channel_id: str = "dingtalk"
    chat_type: str = "group"
    open_conversation_id: str = ""
    sender_staff_id: str = ""
    physical_user_id: str = ""

    @property
    def container_kind(self) -> str:
        return self.chat_type

    def get_container_id(self) -> str:
        return self.open_conversation_id or self.sender_staff_id


@dataclass(frozen=True)
class TelegramDeliveryTarget(DeliveryTarget):
    """Telegram 投递地址."""
    channel_id: str = "telegram"
    chat_id: int = 0                       # 群: <0；私聊: >0
    physical_user_id: str = ""

    @property
    def container_kind(self) -> str:
        return "group" if self.chat_id < 0 else "p2p"

    def get_container_id(self) -> str:
        return str(self.chat_id)


@dataclass(frozen=True)
class DiscordDeliveryTarget(DeliveryTarget):
    """Discord 投递地址."""
    channel_id: str = "discord"
    chat_type: str = "group"
    target_channel_id: int = 0
    physical_user_id: str = ""

    @property
    def container_kind(self) -> str:
        return self.chat_type

    def get_container_id(self) -> str:
        return str(self.target_channel_id)


@dataclass(frozen=True)
class SlackDeliveryTarget(DeliveryTarget):
    """Slack delivery target."""
    channel_id: str = "slack"
    chat_type: str = "group"
    target_channel_id: str = ""
    thread_ts: str = ""
    physical_user_id: str = ""

    @property
    def container_kind(self) -> str:
        return self.chat_type

    def get_container_id(self) -> str:
        if self.thread_ts:
            return f"{self.target_channel_id}:{self.thread_ts}"
        return self.target_channel_id


@dataclass(frozen=True)
class WhatsAppDeliveryTarget(DeliveryTarget):
    """WhatsApp 投递地址."""
    channel_id: str = "whatsapp"
    target_jid: str = ""

    @property
    def container_kind(self) -> str:
        return "group" if self.target_jid.endswith("@g.us") else "p2p"

    def get_container_id(self) -> str:
        return self.target_jid


@dataclass(frozen=True)
class WechatDeliveryTarget(DeliveryTarget):
    """微信投递地址（仅私聊，无群聊）."""
    channel_id: str = "wechat"
    user_id: str = ""

    @property
    def container_kind(self) -> str:
        return "p2p"

    def get_container_id(self) -> str:
        return self.user_id


# ── 工厂函数 ──


def make_delivery_target(
    channel_id: str,
    *,
    chat_id: str = "",
    receive_id: str = "",
    physical_user_id: str = "",
    ws_id: str = "",
    **kwargs: Any,
) -> DeliveryTarget:
    """按 channel_id 构造对应渠道的 DeliveryTarget 子类实例。

    各渠道只传自己需要的字段，其余由子类默认值填充。
    """
    _ch = channel_id or "web"
    if _ch in ("web",):
        return WebDeliveryTarget(channel_id=_ch, ws_id=ws_id)
    if _ch in ("tui",):
        return TuiDeliveryTarget(channel_id=_ch, ws_id=ws_id)
    if _ch in ("acp",):
        return AcpDeliveryTarget(channel_id=_ch, request_id=kwargs.get("request_id", ""))
    if _ch in ("xiaoyi",):
        return XiaoyiDeliveryTarget(
            channel_id=_ch,
            agent_id=physical_user_id,
            push_id=kwargs.get("push_id", ""),
            xiaoyi_session_id=kwargs.get("xiaoyi_session_id", "") or chat_id,
            conversation_id=kwargs.get("conversation_id", ""),
            url_key=kwargs.get("url_key", ""),
        )
    if _ch in ("feishu", "feishu_enterprise"):
        return FeishuDeliveryTarget(
            channel_id=_ch,
            chat_type="group" if chat_id else "p2p",
            chat_id=chat_id,
            receive_id=receive_id or chat_id or physical_user_id,
            id_type="chat_id" if chat_id else "open_id",
            physical_user_id=physical_user_id,
        )
    if _ch in ("wecom",):
        return WecomDeliveryTarget(
            channel_id=_ch,
            chat_type="group" if chat_id else "p2p",
            chat_id=chat_id,
            physical_user_id=physical_user_id,
        )
    if _ch in ("dingtalk",):
        return DingTalkDeliveryTarget(
            channel_id=_ch,
            physical_user_id=physical_user_id,
        )
    if _ch in ("telegram",):
        return TelegramDeliveryTarget(
            channel_id=_ch,
            chat_id=int(chat_id or "0"),
            physical_user_id=physical_user_id,
        )
    if _ch in ("discord",):
        return DiscordDeliveryTarget(
            channel_id=_ch,
            physical_user_id=physical_user_id,
        )
    if _ch in ("slack",):
        return SlackDeliveryTarget(
            channel_id=_ch,
            chat_type=kwargs.get("chat_type", "group"),
            target_channel_id=chat_id or receive_id,
            thread_ts=kwargs.get("thread_ts", ""),
            physical_user_id=physical_user_id,
        )
    if _ch in ("whatsapp",):
        return WhatsAppDeliveryTarget(
            channel_id=_ch,
            target_jid=chat_id or receive_id,
        )
    if _ch in ("wechat",):
        return WechatDeliveryTarget(
            channel_id=_ch,
            user_id=physical_user_id or receive_id,
        )
    # fallback: 未知渠道，返回基础实例（不推荐）
    return WebDeliveryTarget(channel_id=_ch, ws_id=ws_id)
