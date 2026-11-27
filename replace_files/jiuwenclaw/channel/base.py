# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import logging
import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from jiuwenclaw.schema.message import Message


logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    pass


class ChannelType(str, Enum):
    """Channel 类型枚举."""
    ACP = "acp"
    WEB = "web"
    FEISHU = "feishu"
    XIAOYI = "xiaoyi"
    DINGTALK = "dingtalk"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    WECOM = "wecom"
    WECHAT = "wechat"
    CLI = "tui"


@dataclass
class ChannelMetadata:
    """Channel 元数据."""

    channel_id: str
    source: str
    user_id: str | None = None
    extra: dict[str, Any] | None = None


class RobotMessageRouter:
    """管理整个系统的入站（从通道到机器人）和出站（从机器人到通道）消息队列，并提供出站消息的订阅/分发机制。"""
    def __init__(self):
        self._user_messages: asyncio.Queue[Message] = asyncio.Queue()
        self._robot_messages: asyncio.Queue[Message] = asyncio.Queue()
        self._channel_subscriptions: dict[str, list[Callable[[Message], Awaitable[None]]]] = {}
        self._is_active = False

    async def route_user_message(self, msg: Message) -> None:
        """将接收到的消息放入user_messages队列，等待机器人处理。"""
        await self._user_messages.put(msg)

    async def wait_for_user_message(self) -> Message:
        """阻塞地从user_messages队列中取出一条消息进行处理。"""
        return await self._user_messages.get()

    async def queue_robot_message(self, msg: Message) -> None:
        """将生成的回复消息放入robot_messages队列。"""
        await self._robot_messages.put(msg)

    async def wait_for_robot_message(self) -> Message:
        """阻塞地从robot_messages队列取消息，主要用于调试或直接消费（但框架通常使用订阅分发机制）。"""
        return await self._robot_messages.get()

    def register_channel_subscription(
        self,
        channel: str,
        callback: Callable[[Message], Awaitable[None]]
    ) -> None:
        """允许通道（或其他组件）注册一个异步回调函数，专门接收目标为特定通道ID的出站消息。"""
        if channel not in self._channel_subscriptions:
            self._channel_subscriptions[channel] = []
        self._channel_subscriptions[channel].append(callback)

    async def dispatch_robot_messages(self) -> None:
        """
        持续监听robot_messages队列，将每条消息分发给对应通道的订阅回调。
        """
        self._is_active = True
        while self._is_active:
            try:
                msg = await asyncio.wait_for(self._robot_messages.get(), timeout=1.0)
                subscribers = self._channel_subscriptions.get(msg.channel_id, [])
                for callback in subscribers:
                    try:
                        await callback(msg)
                    except Exception as e:
                        logger.error(f"Error dispatching to {msg.channel_id}: {e}")
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the dispatcher loop."""
        self._is_active = False

    @property
    def pending_incoming_count(self) -> int:
        """待处理的入站消息数量"""
        return self._user_messages.qsize()

    @property
    def pending_outgoing_count(self) -> int:
        """待发送的出站消息数量"""
        return self._robot_messages.qsize()


class BaseChannel(ABC):
    """
    Channel实现的抽象基类。

    每个Channel都应该实现这个接口
    以集成到纳米机器人消息总线中。
    """

    name: str = "base"

    def __init__(self, config: Any, router: RobotMessageRouter):
        """
        初始化Channel
        """
        self.config = config
        self.bus = router
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """
        启动Channel并开始监听消息

        一个长期运行的异步任务，需要：
        1. 连接到聊天平台
        2. 监听传入消息
        3. 通过_handle_message()将消息转发到总线
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止Channel并清理资源"""
        pass

    @abstractmethod
    async def send(self, msg: Message) -> None:
        """
        通过Channel发送消息
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """
        检查发送者是否被允许使用此机器人
        """
        allow_list = getattr(self.config, "allow_from", [])

        # If no allow list, allow everyone
        if not allow_list:
            return True

        sender_str = str(sender_id)
        if sender_str in allow_list:
            return True
        if "|" in sender_str:
            for part in sender_str.split("|"):
                if part and part in allow_list:
                    return True
        return False

    async def _handle_message(
            self,
            chat_id: str,
            content: str,
            metadata: dict[str, Any] | None = None
    ) -> None:

        msg = Message(
            id=chat_id,
            type="req",
            channel_id=self.name,
            session_id=str(chat_id),
            params={'content': content},
            timestamp=time.time(),
            ok=True,
            metadata=metadata
        )

        await self.bus.route_user_message(msg)

    @property
    def is_running(self) -> bool:
        """Check if the channel_id is running."""
        return self._running

