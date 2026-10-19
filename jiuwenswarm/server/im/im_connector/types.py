"""用户态 IM 连接器的通道无关数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

ChannelTargetKind = Literal["group", "user"]


@dataclass(frozen=True)
class ChannelMeta:
    """通道静态元数据。

    字段：
        id: 通道标识，如 ``dingtalk`` / ``feishu`` / ``welink``。
        label: 展示名。
        target_kinds: 支持的会话类型。
    """

    id: str
    label: str
    target_kinds: tuple[str, ...] = ("group", "user")


@dataclass(frozen=True)
class ChannelTarget:
    """通道内的一个会话（群或单聊）。

    字段：
        kind: ``group`` 或 ``user``。
        external_id: 平台侧会话标识；拉消息只认这个字段。
        title: 会话标题，可空。
        conversation_id: 托管落库主键，连接器不得依赖。
    """

    kind: Literal["group", "user"]
    external_id: str
    title: Optional[str] = None
    conversation_id: Optional[str] = None

    def to_im_conversation(self, channel_id: str) -> "ImConversation":
        return ImConversation(
            channel_id=channel_id,
            external_id=self.external_id,
            target_kind=self.kind,
            title=self.title,
        )


@dataclass
class ImMessage:
    """归一化后的一条 IM 消息。

    字段：
        channel_id: 通道 id。
        msg_id: 平台消息 id。
        conversation_external_id: 所属会话的平台 id。
        sender_account: 发送方平台账号（钉钉优先 openDingTalkId）。
        sender_name: 发送方展示名。
        content_text: 文本正文。
        sent_at: 发送时间，毫秒时间戳。
        content_type: 原始消息类型，可空。
        direction: ``inbound`` / ``outbound``。
        is_self: 是否本人；身份未解析时为 ``None``。
    """

    channel_id: str
    msg_id: str
    conversation_external_id: str
    sender_account: Optional[str] = None
    sender_name: Optional[str] = None
    content_text: str = ""
    sent_at: int = 0
    content_type: Optional[str] = None
    direction: Literal["inbound", "outbound"] = "inbound"
    is_self: Optional[bool] = None


@dataclass
class ImConversation:
    """归一化会话摘要。"""

    channel_id: str
    external_id: str
    target_kind: Literal["group", "user"] = "group"
    title: Optional[str] = None


@dataclass(frozen=True)
class FetchOptions:
    """``fetch_messages`` 入参。

    字段：
        count: 本页期望条数。
        before_ms / after_ms: 时间窗（毫秒）；通道不支持时可忽略。
        message_id: WeLink 等按消息 id 翻页的锚点。
        page_token: 钉钉 / 飞书等平台分页 token，禁止用 msg_id 冒充。
        query_direction: WeLink 翻页方向；其它通道可忽略。
    """

    count: int = 50
    before_ms: Optional[int] = None
    after_ms: Optional[int] = None
    message_id: Optional[str] = None
    page_token: Optional[str] = None
    query_direction: Optional[int] = None


@dataclass
class MessagePage:
    """``fetch_messages`` 出参。

    字段：
        messages: 本页消息。
        next_page_token: 下一页平台 token；无更多页时为 ``None``。
        has_more: 平台声明是否还有后续。无该字段时为 ``False``。
    """

    messages: list[ImMessage]
    next_page_token: Optional[str] = None
    has_more: bool = False


@dataclass(frozen=True)
class SendOptions:
    """``send_message`` 可选入参。

    字段：
        reply_prefix: 覆盖默认数字分身前缀；``None`` 表示用默认模板。
        mention_sender: 是否 @ 原发送人；``None`` 表示群默认开、单聊默认关。
        extra: 通道附加参数，如 ``source_message``、``group_config``。
    """

    reply_prefix: Optional[str] = None
    mention_sender: Optional[bool] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    """``send_message`` 出参。

    字段：
        ok: 是否发送成功。
        external_message_id: 平台消息 id，回包没有则为 ``None``。
        error_code / error_message: 失败原因。
        raw: 原始回包，便于排障。
    """

    ok: bool
    external_message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TestResult:
    """``test_connection`` 出参。"""

    ok: bool
    message: str = ""
    error_code: Optional[str] = None


@dataclass(frozen=True)
class Identity:
    """``resolve_identity`` 出参。

    字段：
        account: 用于前缀与默认对齐的主账号（钉钉优先 openDingTalkId）。
        display_name: 展示名。
        extra: 至少可含 ``open_ids``（全部可用于 ``is_self`` 的 id）。
    """

    account: Optional[str] = None
    display_name: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelPerson:
    """``search_persons`` 的单条出参。"""

    account: str
    display_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyContext:
    """``format_outgoing_reply`` 入参中的回复上下文。"""

    channel_id: str
    target: ChannelTarget
    source_message: ImMessage
    reply_content: str
    extra: dict[str, Any] = field(default_factory=dict)
