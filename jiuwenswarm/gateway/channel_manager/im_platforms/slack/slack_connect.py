"""Slack channel implementation based on Slack Bolt Socket Mode."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import (
    BaseChannel,
    ChannelMetadata,
    RobotMessageRouter,
)
from jiuwenswarm.gateway.routing.keys import SlackDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)

try:
    from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False
    AsyncApp = None  # type: ignore[assignment,misc]
    AsyncSocketModeHandler = None  # type: ignore[assignment,misc]


_LEADING_MENTION_RE = re.compile(r"^\s*<@[A-Z0-9]+>\s*", re.IGNORECASE)
_MAX_SLACK_TEXT_LENGTH = 4000
_MAX_SEEN_EVENTS = 1024


@dataclass
class SlackChannelConfig:
    """Runtime configuration for the Slack channel."""

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    allow_from: list[str] = field(default_factory=list)
    allowed_channel_ids: list[str] = field(default_factory=list)
    default_channel_id: str = ""
    reply_in_thread: bool = True


class SlackChannel(BaseChannel):
    """Slack Bot channel using Bolt's asynchronous Socket Mode adapter."""

    name = "slack"

    def __init__(self, config: SlackChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: SlackChannelConfig = config
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._seen_event_ids: set[str] = set()
        self._seen_event_order: deque[str] = deque()

    @property
    def channel_id(self) -> str:
        return self.name

    @property
    def clients(self) -> set[Any]:
        return set()

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if not SLACK_AVAILABLE:
            logger.error("Slack SDK not installed. Run: pip install slack-bolt")
            return
        if not self.config.enabled:
            logger.warning("SlackChannel is disabled (enabled=false)")
            return
        if not self.config.bot_token.strip():
            logger.error("SlackChannel missing bot_token")
            return
        if not self.config.app_token.strip():
            logger.error("SlackChannel missing app_token")
            return
        if self._running:
            logger.warning("SlackChannel is already running")
            return

        app = AsyncApp(token=self.config.bot_token.strip())
        app.event("app_mention")(self._handle_app_mention)
        app.event("message")(self._handle_message_event)

        handler = AsyncSocketModeHandler(app, self.config.app_token.strip())
        self._app = app
        self._handler = handler
        self._client = app.client
        self._running = True

        try:
            await handler.start_async()
        except Exception as exc:  # noqa: BLE001
            logger.error("SlackChannel start failed: %s", exc, exc_info=True)
            raise
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
        handler = self._handler
        self._handler = None
        self._app = None
        self._client = None
        if handler is not None:
            try:
                await handler.close_async()
            except Exception as exc:  # noqa: BLE001
                logger.warning("SlackChannel stop failed: %s", exc)
        logger.info("SlackChannel stopped")

    async def send(
        self, msg: Message, *, routing_target: RoutingTarget | None = None
    ) -> None:
        if self._client is None:
            return
        if msg.event_type == EventType.CHAT_DELTA:
            return

        content = self._extract_outgoing_text(msg)
        if not content:
            return

        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id:
            logger.warning("SlackChannel send skipped: missing target channel id")
            return

        for chunk in self._split_text(content):
            kwargs: dict[str, Any] = {"channel": channel_id, "text": chunk}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            try:
                await self._client.chat_postMessage(**kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SlackChannel send failed: %s", exc)
                return

    async def _handle_app_mention(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        await self._handle_slack_event(event, body, is_dm=False)

    async def _handle_message_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        if str(event.get("channel_type") or "") != "im":
            return
        await self._handle_slack_event(event, body, is_dm=True)

    async def _handle_slack_event(
        self,
        event: dict[str, Any],
        body: dict[str, Any],
        *,
        is_dm: bool,
    ) -> None:
        if not self._running:
            return
        if not isinstance(event, dict) or not isinstance(body, dict):
            return
        if event.get("subtype") or event.get("bot_id") or event.get("bot_profile"):
            return

        user_id = str(event.get("user") or "").strip()
        channel_id = str(event.get("channel") or "").strip()
        if not user_id or not channel_id or not self.is_allowed(user_id):
            return
        if not is_dm and self.config.allowed_channel_ids:
            if channel_id not in self.config.allowed_channel_ids:
                return

        event_id = str(
            body.get("event_id") or event.get("client_msg_id") or event.get("ts") or ""
        ).strip()
        if event_id and not self._remember_event(event_id):
            return

        text = str(event.get("text") or "").strip()
        if not is_dm:
            text = _LEADING_MENTION_RE.sub("", text, count=1).strip()
        if not text:
            return

        team = body.get("team")
        team_id = str(
            body.get("team_id")
            or (team.get("id") if isinstance(team, dict) else "")
            or event.get("team")
            or ""
        ).strip()
        message_ts = str(event.get("ts") or "").strip()
        root_thread_ts = str(event.get("thread_ts") or message_ts).strip()
        reply_thread_ts = ""
        if is_dm:
            reply_thread_ts = str(event.get("thread_ts") or "").strip()
        elif self.config.reply_in_thread:
            reply_thread_ts = root_thread_ts

        if is_dm:
            session_id = f"slack_{team_id or 'default'}_{channel_id}_{user_id}"
        else:
            session_id = f"slack_{team_id or 'default'}_{channel_id}_{root_thread_ts}"

        req = Message(
            id=event_id or f"slack-{int(time.time() * 1000)}",
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params={"content": text, "query": text},
            timestamp=time.time(),
            ok=True,
            provider="slack",
            chat_id=channel_id,
            user_id=user_id,
            req_method=ReqMethod.CHAT_SEND,
            metadata={
                "user_id": user_id,
                "slack_event_id": event_id,
                "slack_team_id": team_id,
                "slack_channel_id": channel_id,
                "slack_channel_type": "im"
                if is_dm
                else str(event.get("channel_type") or "channel"),
                "slack_user_id": user_id,
                "slack_message_ts": message_ts,
                "slack_thread_ts": reply_thread_ts,
            },
        )

        if self._on_message_cb is not None:
            result = self._on_message_cb(req)
            if asyncio.iscoroutine(result):
                await result
        else:
            await self.bus.route_user_message(req)

    def _remember_event(self, event_id: str) -> bool:
        if event_id in self._seen_event_ids:
            return False
        if len(self._seen_event_order) >= _MAX_SEEN_EVENTS:
            oldest = self._seen_event_order.popleft()
            self._seen_event_ids.discard(oldest)
        self._seen_event_order.append(event_id)
        self._seen_event_ids.add(event_id)
        return True

    def _extract_delivery(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
    ) -> tuple[str, str]:
        delivery = routing_target.delivery if routing_target is not None else None
        if isinstance(delivery, SlackDeliveryTarget):
            channel_id = str(delivery.target_channel_id or "").strip()
            if channel_id:
                return channel_id, str(delivery.thread_ts or "").strip()

        metadata = msg.metadata or {}
        channel_id = str(metadata.get("slack_channel_id") or "").strip()
        thread_ts = str(metadata.get("slack_thread_ts") or "").strip()
        if channel_id:
            return channel_id, thread_ts

        session_id = str(msg.session_id or "")
        if session_id.startswith("slack_"):
            parts = session_id.split("_", 4)
            if len(parts) >= 4:
                channel_id = parts[2].strip()
                session_target = parts[3].strip()
                if channel_id:
                    parsed_thread = session_target if "." in session_target else ""
                    return channel_id, parsed_thread

        return self.config.default_channel_id.strip(), ""

    @staticmethod
    def _extract_outgoing_text(msg: Message) -> str:
        payload = getattr(msg, "payload", None) or {}
        if msg.event_type == EventType.HEARTBEAT_RELAY and isinstance(payload, dict):
            heartbeat = payload.get("heartbeat")
            if heartbeat:
                return str(heartbeat).strip()

        if isinstance(payload, dict):
            if "content" in payload:
                content = payload.get("content")
                if isinstance(content, dict):
                    return str(content.get("output", content)).strip()
                return str(content or "").strip()
            if payload.get("error"):
                return str(payload.get("error")).strip()
        if msg.params and "content" in msg.params:
            return str(msg.params.get("content") or "").strip()
        if isinstance(msg.payload, str):
            return msg.payload.strip()
        return ""

    @staticmethod
    def _split_text(content: str) -> list[str]:
        return [
            content[slice(index, index + _MAX_SLACK_TEXT_LENGTH)]
            for index in range(0, len(content), _MAX_SLACK_TEXT_LENGTH)
        ]

    def get_metadata(self) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="slack",
            extra={
                "default_channel_id": self.config.default_channel_id,
                "allowed_channel_ids": list(self.config.allowed_channel_ids),
                "reply_in_thread": self.config.reply_in_thread,
            },
        )
