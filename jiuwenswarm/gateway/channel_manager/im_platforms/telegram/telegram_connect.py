# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""TelegramChannel - Telegram Bot 通道实现."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import logging

from jiuwenswarm.gateway.channel_manager.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.routing.keys import DeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        filters,
        ContextTypes,
    )

    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = None
    Application = None
    ContextTypes = None


@dataclass
class TelegramChannelConfig:
    """Telegram 通道配置."""

    enabled: bool = False
    bot_token: str = ""  # Telegram Bot Token from @BotFather
    allow_from: list[str] = field(default_factory=list)  # 允许的 Telegram user_id 列表
    parse_mode: str = "Markdown"  # 消息解析模式: Markdown, HTML, None
    group_chat_mode: str = "mention"  # 群聊模式: all, mention, reply, off


class TelegramChannel(BaseChannel):
    """
    Telegram Bot 通道.

    使用 Telegram Bot API 接收和发送消息.
    需要:
    - 来自 @BotFather 的 Bot Token
    - 可选: 配置允许访问的用户白名单
    """

    name = "telegram"

    def __init__(self, config: TelegramChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: TelegramChannelConfig = config
        self._application: Any = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._chat_sessions: dict[int, str] = {}  # chat_id -> session_id 映射

    @property
    def channel_id(self) -> str:
        """ChannelManager 按 channel_id 注册与派发."""
        return self.name

    @property
    def clients(self) -> set[Any]:
        """兼容 BaseChannel 接口."""
        return set()

    def on_message(self, callback: Callable[[Message], None]) -> None:
        """ChannelManager 注册: 收到消息时调用 callback."""
        self._on_message_cb = callback

    async def start(self) -> None:
        """启动 Telegram Bot."""
        if not TELEGRAM_AVAILABLE:
            logger.error(
                "Telegram SDK not installed. Run: pip install python-telegram-bot"
            )
            return

        if not self.config.enabled:
            logger.warning("TelegramChannel 未启用（enabled=False）")
            return

        if not self.config.bot_token:
            logger.error("Telegram bot_token not configured")
            return

        if self._running:
            logger.warning("TelegramChannel 已在运行")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        try:
            # 创建 Telegram Application
            self._application = (
                Application.builder().token(self.config.bot_token).build()
            )

            # 注册命令处理器
            self._application.add_handler(CommandHandler("start", self._start_command))
            self._application.add_handler(CommandHandler("help", self._help_command))

            # 注册消息处理器 (文本消息)
            self._application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
            )

            # 初始化并启动 bot
            await self._application.initialize()
            await self._application.start()

            # 在后台运行 polling
            await self._application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
            )

            logger.info("Telegram Bot 已启动")

            # 持续运行直到停止
            while self._running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error("Telegram Bot 启动失败: %s", e)
            self._running = False
            raise

    async def stop(self) -> None:
        """停止 Telegram Bot."""
        self._running = False

        if self._application:
            try:
                if self._application.updater.running:
                    await self._application.updater.stop()
                await self._application.stop()
                await self._application.shutdown()
            except Exception as e:
                logger.warning("Error stopping Telegram Bot: %s", e)

        logger.info("Telegram Bot 已停止")

    async def send(self, msg: Message, *, routing_target: RoutingTarget | None = None) -> None:
        """通过 Telegram 发送消息."""
        if not self._application or not self._running:
            logger.warning("Telegram Bot not initialized or not running")
            return

        try:
            # 从 session_id 或 metadata 获取 chat_id
            chat_id = self._get_chat_id_from_message(msg)
            if not chat_id:
                logger.warning("Telegram send: 无法确定 chat_id")
                return

            # 提取消息内容
            content = self._extract_content(msg)
            if not content:
                logger.warning("Telegram send: content 为空，跳过发送")
                return

            # 发送消息
            parse_mode = (
                self.config.parse_mode if self.config.parse_mode != "None" else None
            )

            try:
                await self._application.bot.send_message(
                    chat_id=chat_id, text=content, parse_mode=parse_mode
                )
            except Exception as send_error:
                # 仅在 parse_mode 非空且错误涉及解析时重试
                error_str = str(send_error)
                if parse_mode and (
                        "parse" in error_str.lower() or "entity" in error_str.lower()
                ):
                    logger.warning(
                        f"Telegram Markdown parse error, retrying without parse_mode: {send_error}"
                    )
                    await self._application.bot.send_message(
                        chat_id=chat_id, text=content, parse_mode=None
                    )
                else:
                    raise

            logger.debug("Telegram message sent to chat_id=%s", chat_id)

        except Exception as e:
            logger.error(f"Error sending Telegram message: {type(e).__name__}: {e}")

    def _get_chat_id_from_message(self, msg: Message) -> int | None:
        """从 Message 中提取 chat_id."""
        # 优先从 metadata 获取
        if msg.metadata and "chat_id" in msg.metadata:
            return int(msg.metadata["chat_id"])

        # 从 session_id 解析 (格式: "telegram_{chat_id}")
        if msg.session_id and msg.session_id.startswith("telegram_"):
            try:
                return int(msg.session_id.split("_")[1])
            except (IndexError, ValueError):
                pass

        return None

    def _extract_content(self, msg: Message) -> str:
        """从 Message 中提取文本内容."""
        # Gateway/Agent 响应在 payload.content
        content = (
                (msg.params or {}).get("content")
                or (getattr(msg, "payload") or {}).get("content")
                or ""
        )

        # 处理字典格式
        if isinstance(content, dict):
            content = content.get("output", str(content))

        return str(content).strip()

    async def _start_command(
            self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """处理 /start 命令."""
        if not update.effective_user or not update.effective_chat:
            return

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        # 检查权限
        if not self.is_allowed(str(user_id)):
            await update.message.reply_text("抱歉，您没有权限使用此机器人。")
            return

        welcome_msg = (
            "欢迎使用 JiuWenSwarm 机器人! 🤖\n\n"
            "您可以直接发送消息与我对话。\n"
            "使用 /help 查看帮助信息。"
        )
        await update.message.reply_text(welcome_msg)
        logger.info(f"Telegram /start from user_id={user_id} chat_id={chat_id}")

    async def _help_command(
            self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """处理 /help 命令."""
        help_msg = (
            "JiuWenSwarm 机器人帮助 📚\n\n"
            "命令:\n"
            "/start - 开始对话\n"
            "/help - 显示帮助\n\n"
            "您可以直接发送文本消息与我对话。"
        )
        await update.message.reply_text(help_msg)

    async def _handle_message(
            self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """处理接收到的消息."""
        try:
            if (
                    not update.message
                    or not update.effective_user
                    or not update.effective_chat
            ):
                return

            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            message_id = update.message.message_id
            text = update.message.text or ""

            # 检查权限
            if not self.is_allowed(str(user_id)):
                logger.warning(f"Telegram message from unauthorized user: {user_id}")
                return

            # 检查是否为群聊
            is_group_chat = update.effective_chat.type in ["group", "supergroup"]

            # 群聊模式检查
            if is_group_chat:
                group_mode = self.config.group_chat_mode

                # off 模式: 不响应群聊消息
                if group_mode == "off":
                    logger.debug(
                        "Telegram group chat mode is 'off', ignoring message from chat_id=%s",
                        chat_id
                    )
                    return

                # mention 模式: 只响应 @机器人 的消息
                if group_mode == "mention":
                    bot_username = context.bot.username
                    if not bot_username:
                        logger.warning(
                            "Cannot check mentions: bot username not available"
                        )
                        return

                    # 检查是否 @ 了机器人
                    mention_text = f"@{bot_username}"
                    if mention_text not in text:
                        logger.debug(
                            f"Telegram group chat mode is 'mention', message doesn't mention bot, ignoring"
                        )
                        return

                    # 移除 @mention 从文本中
                    text = text.replace(mention_text, "").strip()

                # reply 模式: 只响应回复机器人的消息
                elif group_mode == "reply":
                    if not update.message.reply_to_message:
                        logger.debug(
                            f"Telegram group chat mode is 'reply', message is not a reply, ignoring"
                        )
                        return

                    # 检查是否回复的是机器人的消息
                    if update.message.reply_to_message.from_user.id != context.bot.id:
                        logger.debug(
                            f"Telegram group chat mode is 'reply', not replying to bot, ignoring"
                        )
                        return

                # all 模式: 响应所有消息（默认行为）

            # 对原消息回应一个表情，表示正在处理
            try:
                await update.message.set_reaction("👀")  # 使用眼睛表情表示"正在查看"
            except Exception as e:
                logger.debug("Failed to set reaction: %s", e)

            # 生成或获取 session_id
            session_id = self._chat_sessions.get(chat_id)
            if not session_id:
                session_id = f"telegram_{chat_id}"
                self._chat_sessions[chat_id] = session_id

            # 创建 Message 对象
            user_message = Message(
                id=str(message_id),
                type="req",
                channel_id=self.channel_id,
                session_id=session_id,
                params={"content": text, "query": text},
                timestamp=time.time(),
                ok=True,
                req_method=ReqMethod.CHAT_SEND,
                metadata={
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "message_id": message_id,
                    "username": update.effective_user.username,
                    "is_group_chat": is_group_chat,
                },
            )

            # 发送到 Gateway 或 Router
            if self._on_message_cb:
                result = self._on_message_cb(user_message)
                if asyncio.iscoroutine(result):
                    await result
            else:
                await self.bus.route_user_message(user_message)

            logger.info(
                f"Telegram message received: "
                f"user_id={user_id} chat_id={chat_id} is_group={is_group_chat} text={text[:50]}"
            )

        except Exception as e:
            logger.error(f"Error processing Telegram message: {e}")

    def get_metadata(self) -> ChannelMetadata:
        """获取 Channel 元数据."""
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="telegram",
            extra={
                "bot_token_configured": bool(
                    self.config.bot_token and self.config.bot_token.strip()
                ),
                "parse_mode": self.config.parse_mode,
            },
        )
