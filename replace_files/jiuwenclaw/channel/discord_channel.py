"""Discord channel implementation based on discord.py."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenclaw.channel.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenclaw.schema.message import EventType, Message, ReqMethod

logger = logging.getLogger(__name__)

try:
    import discord

    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None  # type: ignore[assignment]


@dataclass
class DiscordChannelConfig:
    enabled: bool = False
    bot_token: str = ""
    application_id: str = ""
    guild_id: str = ""
    channel_id: str = ""
    block_dm: bool = False
    allow_from: list[str] = field(default_factory=list)


class DiscordChannel(BaseChannel):
    """Discord Bot channel."""

    name = "discord"

    def __init__(self, config: DiscordChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: DiscordChannelConfig = config
        self._client: Any = None
        self._on_message_cb: Callable[[Message], Any] | None = None

    @property
    def channel_id(self) -> str:
        return self.name

    @property
    def clients(self) -> set[Any]:
        return set()

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if not DISCORD_AVAILABLE:
            logger.error("Discord SDK not installed. Run: pip install discord.py")
            return
        if not self.config.enabled:
            logger.warning("DiscordChannel is disabled (enabled=false)")
            return
        if not self.config.bot_token.strip():
            logger.error("DiscordChannel missing bot_token")
            return
        if self._running:
            logger.warning("DiscordChannel is already running")
            return

        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.dm_messages = True
        intents.message_content = True

        app_id: int | None = None
        app_id_raw = (self.config.application_id or "").strip()
        if app_id_raw.isdigit():
            app_id = int(app_id_raw)

        client = discord.Client(intents=intents, application_id=app_id)
        self._client = client
        self._running = True

        @client.event
        async def on_ready() -> None:
            user = getattr(client, "user", None)
            logger.info("Discord bot ready: %s (%s)", getattr(user, "name", "unknown"), getattr(user, "id", "unknown"))

        @client.event
        async def on_message(message: Any) -> None:
            await self._handle_discord_message(message)

        try:
            await client.start(self.config.bot_token.strip())
        except Exception as e:  # noqa: BLE001
            logger.error("DiscordChannel start failed: %s", e, exc_info=True)
            raise
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
        client = self._client
        self._client = None
        if client is not None:
            try:
                if not client.is_closed():
                    await client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("DiscordChannel stop failed: %s", e)
        logger.info("DiscordChannel stopped")

    async def send(self, msg: Message) -> None:
        if self._client is None or self._client.is_closed():
            return

        content = self._extract_outgoing_text(msg)
        if not content:
            return

        target_channel_id = self._extract_target_channel_id(msg)
        if target_channel_id is None:
            logger.warning("DiscordChannel send skipped: missing target channel id")
            return

        channel = self._client.get_channel(target_channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(target_channel_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("DiscordChannel fetch channel failed: %s", e)
                return

        try:
            await channel.send(content)
        except Exception as e:  # noqa: BLE001
            logger.warning("DiscordChannel send failed: %s", e)

    async def _handle_discord_message(self, message: Any) -> None:
        if not self._running:
            return
        if message is None or getattr(message.author, "bot", False):
            return

        author_id = str(getattr(message.author, "id", "") or "")
        if not author_id:
            return
        if not self.is_allowed(author_id):
            return

        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        if channel is None:
            return

        author_name = str(getattr(message.author, "name", "") or "")
        author_nickname = str(getattr(message.author, "global_name", "") or "")
        guild_id = str(getattr(guild, "id", "") or "")
        channel_id = str(getattr(channel, "id", "") or "")
        cfg_guild = (self.config.guild_id or "").strip()
        cfg_channel = (self.config.channel_id or "").strip()
        # Guild/channel filters apply only to server messages. DMs have no guild
        if guild_id:  # message from server
            if cfg_guild and guild_id != cfg_guild:
                return
            if cfg_channel and channel_id != cfg_channel:
                return
        elif self.config.block_dm:  # blocked direct message
            logger.info(
                "Discord direct message ignored, from user %s (name=%s, id=%s)", author_nickname, author_name, author_id
            )
            await self._add_reaction_emoji(message, emoji="🚫")
            return

        text = str(getattr(message, "content", "") or "").strip()
        if not text:
            return

        add_emoji_exception = await self._add_reaction_emoji(message, emoji="👀")
        if add_emoji_exception is not None:
            logger.error(
                "Discord add reaction to message from user %s (name=%s, id=%s) failed: %s",
                author_nickname, author_name, author_id, add_emoji_exception,
            )
        del add_emoji_exception

        session_id = f"discord_{channel_id}_{author_id}"
        req = Message(
            id=str(getattr(message, "id", f"discord-{int(time.time() * 1000)}")),
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params={"content": text, "query": text},
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            metadata={
                "discord_user_id": author_id,
                "discord_username": author_name,
                "discord_global_name": author_nickname,
                "discord_channel_id": channel_id,
                "discord_channel_name": str(getattr(channel, "name", "") or ""),
                "discord_guild_id": guild_id,
                "discord_guild_name": str(getattr(guild, "name", "") or ""),
                "discord_message_id": str(getattr(message, "id", "") or ""),
            },
        )

        if self._on_message_cb is not None:
            result = self._on_message_cb(req)
            if asyncio.iscoroutine(result):
                await result
        else:
            await self.bus.route_user_message(req)

    def _extract_target_channel_id(self, msg: Message) -> int | None:
        meta = msg.metadata or {}
        raw = meta.get("discord_channel_id")
        if raw is not None:
            raw_str = str(raw).strip()
            if raw_str.isdigit():
                return int(raw_str)

        session_id = str(msg.session_id or "")
        if session_id.startswith("discord_"):
            parts = session_id.split("_")
            if len(parts) >= 3 and parts[1].isdigit():
                return int(parts[1])

        fallback = (self.config.channel_id or "").strip()
        if fallback.isdigit():
            return int(fallback)
        return None

    @staticmethod
    def _extract_outgoing_text(msg: Message) -> str:
        payload = getattr(msg, "payload", None) or {}
        if msg.event_type == EventType.HEARTBEAT_RELAY and isinstance(payload, dict) and payload.get("heartbeat"):
            return str(payload.get("heartbeat")).strip()

        if isinstance(payload, dict) and "content" in payload:
            content = payload.get("content")
            if isinstance(content, dict):
                return str(content.get("output", content)).strip()
            return str(content or "").strip()
        if msg.params and "content" in msg.params:
            return str(msg.params.get("content") or "").strip()
        if isinstance(msg.payload, str):
            return msg.payload.strip()
        return ""

    @staticmethod
    async def _add_reaction_emoji(message: Any, emoji: str = "👀") -> Exception | None:
        try:
            await message.add_reaction(emoji)
            return None
        except Exception as e:
            return e

    def get_metadata(self) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="discord",
            extra={
                "application_id": self.config.application_id,
                "guild_id": self.config.guild_id,
                "channel_id": self.config.channel_id,
            },
        )
