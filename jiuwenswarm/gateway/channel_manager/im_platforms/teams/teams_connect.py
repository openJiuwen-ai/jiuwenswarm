# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Microsoft Teams channel (E7 - step 2).

Sends agent replies into a Teams conversation through the Bot Framework
REST surface. Unlike e-mail, Teams genuinely supports buttons and rich
text, so the capability sheet declares them - an ``interactive_card`` from
the E2 pipeline is rendered as a native Adaptive Card (E7 step 1) instead
of being degraded.

The HTTP poster is injected, so the channel is unit-tested without the Bot
Framework SDK or a live tenant.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import BaseChannel, RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.teams.teams_cards import (
    card_attachment,
    conversation_key,
    extract_action,
    strip_mention,
)
from jiuwenswarm.gateway.channel_manager.sdk.capabilities import ChannelCapabilities
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)


@dataclass
class TeamsChannelConfig:
    """Settings for the Teams channel."""

    enabled: bool = False
    app_id: str = ""
    app_password: str = ""
    bot_name: str = ""
    service_url: str = ""
    allow_from: list[str] = field(default_factory=list)


class TeamsChannel(BaseChannel):
    """Deliver agent replies to Microsoft Teams conversations."""

    name = "teams"
    capabilities = ChannelCapabilities(buttons=True, rich_text=True, threads=True)

    def __init__(
        self,
        config: TeamsChannelConfig,
        router: RobotMessageRouter,
        poster: Callable[[str, dict], Any] | None = None,
    ) -> None:
        super().__init__(config, router)
        self._poster = poster
        self._conversations: dict[str, dict[str, str]] = {}

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("TeamsChannel disabled, not starting")
            return
        if not self.config.app_id or not self.config.app_password:
            logger.warning("TeamsChannel missing app_id or app_password, not starting")
            return
        self._running = True
        logger.info("TeamsChannel ready as app %s", self.config.app_id)

    async def stop(self) -> None:
        self._running = False
        self._conversations.clear()

    def remember_conversation(
        self, session_id: str, conversation_id: str, service_url: str
    ) -> None:
        """Record where a session's replies must be posted."""
        self._conversations[str(session_id)] = {
            "conversation_id": str(conversation_id),
            "service_url": str(service_url or self.config.service_url),
        }

    def target_for(self, session_id: str) -> tuple[str, str]:
        """Return (conversation_id, service_url) for a session."""
        stored = self._conversations.get(str(session_id))
        if not stored:
            return "", self.config.service_url
        return stored["conversation_id"], stored["service_url"]

    def build_activity(self, msg: Message) -> dict | None:
        """Compose the Teams activity an agent reply becomes."""
        payload = getattr(msg, "payload", None) or {}
        card_spec = payload.get("interactive_card")
        if card_spec:
            return {
                "type": "message",
                "text": str(card_spec.get("text", "")),
                "attachments": [card_attachment(card_spec)],
            }
        text = str((getattr(msg, "params", None) or {}).get("content", "") or "").strip()
        if not text:
            return None
        return {"type": "message", "text": text}

    async def send(
        self,
        msg: Message,
        *,
        routing_target: RoutingTarget | None = None,
    ) -> None:
        activity = self.build_activity(msg)
        if activity is None:
            return
        conversation_id, service_url = self.target_for(getattr(msg, "session_id", "") or "")
        if not conversation_id or not service_url:
            logger.warning(
                "TeamsChannel has no conversation for session %s", msg.session_id
            )
            return
        if self._poster is None:
            logger.warning("TeamsChannel has no poster configured")
            return
        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
        try:
            result = self._poster(url, activity)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("TeamsChannel send failed: %s", exc)

    def parse_activity(self, activity: dict) -> dict | None:
        """Turn an inbound Teams activity into what the agent needs."""
        if not isinstance(activity, dict):
            return None
        conversation = activity.get("conversation") or {}
        channel_data = activity.get("channelData") or {}
        tenant = (channel_data.get("tenant") or {}).get("id", "")
        conversation_id = str(conversation.get("id", ""))
        if not conversation_id:
            return None
        sender = str((activity.get("from") or {}).get("id", ""))
        action = extract_action(activity.get("value"))
        content = action or strip_mention(
            str(activity.get("text", "")), self.config.bot_name
        )
        if not content:
            return None
        return {
            "sender": sender,
            "session_id": conversation_key(tenant, conversation_id),
            "conversation_id": conversation_id,
            "service_url": str(activity.get("serviceUrl", "")),
            "content": content,
            "is_action": bool(action),
        }