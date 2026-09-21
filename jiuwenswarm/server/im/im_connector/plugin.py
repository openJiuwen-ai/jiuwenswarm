"""用户态 IM 通道插件契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from jiuwenswarm.server.im.im_connector.types import (
    ChannelMeta,
    ChannelPerson,
    ChannelTarget,
    FetchOptions,
    Identity,
    MessagePage,
    ReplyContext,
    SendOptions,
    SendResult,
    TestResult,
)


class ChannelPlugin(ABC):
    """用户态 IM 适配器。统一渠道适配器，屏蔽各平台 CLI 差异，供托管回复与学习拉取调用。"""

    @property
    @abstractmethod
    def meta(self) -> ChannelMeta:
        """功能：返回通道静态信息。

        入参：无。
        出参：``ChannelMeta``（id、展示名、支持的会话类型）。
        """

    @abstractmethod
    async def fetch_messages(
        self,
        target: ChannelTarget,
        options: FetchOptions,
    ) -> MessagePage:
        """功能：按会话拉取一页历史消息。

        入参：
            target: 会话。只用 ``kind`` + ``external_id``，不依赖托管 ``conversation_id``。
            options: 条数、``page_token``（平台分页）或 ``message_id``（WeLink 锚点）。
        出参：
            ``MessagePage``。``messages`` 需尽量填 ``is_self``；有下一页时给出
            ``next_page_token``，不得用消息 id 冒充平台 token。
        """

    @abstractmethod
    async def send_message(
        self,
        target: ChannelTarget,
        content: str,
        options: Optional[SendOptions] = None,
    ) -> SendResult:
        """功能：以当前登录用户向指定会话发消息。

        入参：
            target: 目标会话。
            content: 回复正文（未加前缀）。
            options: 前缀、是否 @ 原发送人、``extra.source_message`` 等。
        出参：
            ``SendResult``。成功时 ``ok=True``；平台回了消息 id 则写入
            ``external_message_id``。
        """

    @abstractmethod
    async def resolve_identity(self) -> Optional[Identity]:
        """功能：解析当前登录身份。

        入参：无。
        出参：
            ``Identity``。``display_name`` 用于数字分身前缀；``account`` /
            ``extra["open_ids"]`` 用于 ``is_self``。未登录或查询失败返回 ``None``。
        """

    @abstractmethod
    async def test_connection(self) -> TestResult:
        """功能：探测 CLI 是否可用。不发送业务消息。

        入参：无。
        出参：``TestResult``（``ok``、说明、错误码）。
        """

    async def search_persons(self, text: str) -> list[ChannelPerson]:
        """功能：按关键字搜人，用于解析单聊对象。

        入参：
            text: 搜索关键字。
        出参：
            ``ChannelPerson`` 列表。不支持时返回空列表。
        """
        return []

    async def discover_conversations(self, *, query_count: int) -> list[ChannelTarget]:
        """功能：发现近期可接入会话。

        入参：
            query_count: 期望条数上限。
        出参：
            ``ChannelTarget`` 列表。不支持时返回空列表。
        """
        return []

    def format_outgoing_reply(
        self,
        content: str,
        context: ReplyContext,
    ) -> str:
        """功能：把待发正文格式化为最终发出文本（前缀、@ 等）。

        入参：
            content: 原始回复正文。
            context: 通道、目标会话、源消息及附加配置。
        出参：
            最终发出字符串。默认原样返回 ``content``。
        """
        return content


__all__ = ["ChannelPlugin"]
