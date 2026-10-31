"""Normalize and accept application-scoped DingTalk direct and group Requests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.configuration import dingtalk_apps
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition


if TYPE_CHECKING:
    from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
    from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService


logger = logging.getLogger(__name__)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing DingTalk {field}")
    return value.strip()


@dataclass(frozen=True, kw_only=True)
class DingTalkInboundEvent:
    receiving_app_id: str
    expected_agent_id: str
    message_id: str
    conversation_id: str
    sender_staff_id: str
    sender_name: str
    text: str
    received_at: datetime
    conversation_type: str = "1"
    receiver_bot_mentioned: bool = False
    mention_ids: tuple[str, ...] = ()
    group_name: str | None = None
    robot_code: str | None = None
    sender_corp_id: str | None = None
    chatbot_corp_id: str | None = None
    platform_created_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in (
            "receiving_app_id", "expected_agent_id", "message_id", "conversation_id",
            "sender_staff_id", "sender_name", "text",
        ):
            _text(getattr(self, field), field)
        if not isinstance(self.received_at, datetime) or self.received_at.tzinfo is None:
            raise ValueError("DingTalk received_at must be timezone-aware")
        if self.platform_created_at is not None and (
            not isinstance(self.platform_created_at, datetime) or self.platform_created_at.utcoffset() is None
            or self.platform_created_at > self.received_at + timedelta(minutes=5)
        ):
            raise ValueError("invalid DingTalk platform creation time")
        if self.conversation_type not in {"1", "2"}:
            raise ValueError("invalid DingTalk conversation type")
        if not isinstance(self.receiver_bot_mentioned, bool):
            raise ValueError("invalid DingTalk mention flag")
        if not isinstance(self.mention_ids, tuple) or any(not isinstance(item, str) or not item.strip()
                                                          for item in self.mention_ids):
            raise ValueError("invalid DingTalk mention IDs")
        if self.group_name is not None and not isinstance(self.group_name, str):
            raise ValueError("invalid DingTalk group name")
        for field in ("robot_code", "sender_corp_id", "chatbot_corp_id"):
            if getattr(self, field) is not None:
                _text(getattr(self, field), field)

    @classmethod
    def from_callback(cls, data: Mapping[str, Any], *, app_id: str, agent_id: str) -> DingTalkInboundEvent:
        conversation_type = str(data.get("conversationType", "")) if isinstance(data, Mapping) else ""
        if (
            not isinstance(data, Mapping) or conversation_type not in {"1", "2"} or data.get("msgtype") != "text"
        ):
            raise ValueError("only DingTalk direct and group text messages are supported")
        content = data.get("text")
        if not isinstance(content, Mapping):
            raise ValueError("invalid DingTalk text")
        staff_id = _text(data.get("senderStaffId"), "senderStaffId")
        mentions = data.get("atUsers", ())
        mention_ids = tuple(dict.fromkeys(
            item.get("dingtalkId") or item.get("staffId")
            for item in mentions
            if isinstance(item, Mapping)
            and isinstance(item.get("dingtalkId") or item.get("staffId"), str)
            and (item.get("dingtalkId") or item.get("staffId")).strip()
        )) if isinstance(mentions, (list, tuple)) else ()
        chatbot_user_id = data.get("chatbotUserId")
        mentions_everyone = any(item.casefold() in {"all", "@_all"} for item in mention_ids)
        # DingTalk's Stream bot-message callback is only delivered for an @ to that bot in a group.
        # Trust this app-scoped callback when its mention metadata is missing or inconsistent.
        bot_mentioned = not mentions_everyone and (
            conversation_type == "2"
            or data.get("isInAtList") is True
            or (isinstance(chatbot_user_id, str) and chatbot_user_id in mention_ids)
        )
        received_at = datetime.now(timezone.utc)
        platform_created_at = None
        created = data.get("createAt")
        if not isinstance(created, bool) and (
            isinstance(created, int) or (isinstance(created, str) and created.isdecimal())
        ):
            try:
                candidate = datetime.fromtimestamp(int(created) / 1000, timezone.utc)
                if candidate.year >= 2000 and candidate <= received_at + timedelta(minutes=5):
                    platform_created_at = candidate
            except (ValueError, OverflowError, OSError):
                pass
        return cls(
            receiving_app_id=app_id, expected_agent_id=agent_id,
            message_id=_text(data.get("msgId"), "msgId"),
            conversation_id=_text(data.get("conversationId"), "conversationId"),
            sender_staff_id=staff_id, sender_name=data.get("senderNick") or staff_id,
            text=_text(content.get("content"), "text"), received_at=received_at,
            conversation_type=conversation_type, receiver_bot_mentioned=bot_mentioned,
            mention_ids=mention_ids,
            group_name=data.get("conversationTitle") if isinstance(data.get("conversationTitle"), str) else None,
            robot_code=data.get("robotCode"), sender_corp_id=data.get("senderCorpId"),
            chatbot_corp_id=data.get("chatbotCorpId"),
            platform_created_at=platform_created_at,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            **asdict(self), "received_at": self.received_at.isoformat(),
            "platform_created_at": self.platform_created_at.isoformat() if self.platform_created_at is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> DingTalkInboundEvent:
        if not isinstance(payload, dict):
            raise ValueError("invalid DingTalk inbound payload")
        fields = dict(payload)
        fields["received_at"] = datetime.fromisoformat(fields["received_at"])
        fields["mention_ids"] = tuple(fields.get("mention_ids", ()))
        if fields.get("platform_created_at") is not None:
            fields["platform_created_at"] = datetime.fromisoformat(fields["platform_created_at"])
        return cls(**fields)


@dataclass(frozen=True, kw_only=True)
class DingTalkBinding:
    app_id: str
    agent_id: str
    robot_code: str
    namespace_id: str = "local"


def configured_dingtalk_bindings(config: Mapping[str, Any] | None = None) -> tuple[DingTalkBinding, ...]:
    if config is None:
        from jiuwenswarm.common.config import get_config
        config = get_config()
    if not isinstance(config, Mapping):
        return ()
    channels = config.get("channels", {})
    if not isinstance(channels, Mapping):
        return ()
    bindings = []
    for app in dingtalk_apps(channels.get("dingtalk")):
        app_id, agent_id = app["client_id"], app["persistent_agent_id"]
        if not app["enabled"] or not app_id or not agent_id:
            continue
        bindings.append(DingTalkBinding(app_id=app_id, agent_id=agent_id, robot_code=app["robot_code"] or app_id))
    return tuple(
        binding for binding in bindings
        if sum(item.app_id == binding.app_id for item in bindings) == 1
        and sum(item.agent_id == binding.agent_id for item in bindings) == 1
    )


class DingTalkInboundHandler:
    """Use the existing IM manager; never construct a separate scheduler or core entry."""

    def __init__(
        self, *, service: PersistentAgentService, sessions: ExecutionRepository,
        agent_manager: AgentManager, config_loader: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._service = service
        self._sessions = sessions
        self._manager = agent_manager
        self._config_loader = config_loader

    async def handle(self, event: DingTalkInboundEvent) -> InboundDisposition:
        return await self._handle(event)

    async def handle_history_message(
        self, binding: DingTalkBinding, message: Mapping[str, Any], *,
        group_id: str, dispatch_after: datetime,
    ) -> InboundDisposition:
        """Accept host-read history without treating user access as Bot membership."""
        try:
            if not isinstance(message, Mapping) or dispatch_after.utcoffset() is None:
                raise ValueError("Invalid history boundary")
            kind = {"user": "human", "robot": "agent"}.get(message.get("sendType"))
            # Unknown sender types and media without text cannot become human requests.
            if kind is None or not isinstance(message.get("text"), str) or not message["text"].strip():
                return InboundDisposition.HANDLED
            message_id = _text(message.get("messageId") or message.get("openMessageId"), "history message ID")
            if message.get("openMessageId", message_id) != message_id:
                raise ValueError("History message identity mismatch")
            conversation_id = _text(
                message.get("openConversationId") or message.get("conversationId"), "history conversation ID",
            )
            for name in ("openConversationId", "conversationId"):
                if message.get(name, conversation_id) != conversation_id:
                    raise ValueError("History conversation identity mismatch")
            sender_id = _text(message.get("senderOpenDingTalkId"), "sender open ID")
            created = message.get("createTime")
            if not isinstance(created, str):
                raise ValueError("Missing history timestamp")
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                # DWS's unqualified conversation timestamps are Shanghai wall time.
                stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            stamp = stamp.astimezone(timezone.utc)
            if stamp.year < 2000:
                raise ValueError("Invalid history timestamp")
            event = DingTalkInboundEvent(
                receiving_app_id=binding.app_id, expected_agent_id=binding.agent_id,
                robot_code=binding.robot_code, message_id=message_id, conversation_id=conversation_id,
                sender_staff_id=f"dingtalk-open:{sender_id}",
                sender_name=message.get("sender") or sender_id,
                text=message["text"], received_at=stamp, conversation_type="2",
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            return InboundDisposition.REJECTED
        return await self._handle(
            event, history_group_id=group_id, history_sender_kind=kind,
            history_dispatch_after=dispatch_after,
        )

    async def _handle(
        self, event: DingTalkInboundEvent, *, history_group_id: str | None = None,
        history_sender_kind: str = "human", history_dispatch_after: datetime | None = None,
    ) -> InboundDisposition:
        from jiuwenswarm.common.im_binding_lock import im_binding_lock

        try:
            async with im_binding_lock("dingtalk", event.receiving_app_id):
                # Reload and validate the binding inside the same lock as the durable queue write.
                return await self._handle_bound_event(
                    event, history_group_id=history_group_id,
                    history_sender_kind=history_sender_kind,
                    history_dispatch_after=history_dispatch_after,
                )
        except Exception as exc:
            logger.warning("[DingTalk] application binding busy: error_type=%s", type(exc).__name__)
            return InboundDisposition.REJECTED

    async def _handle_bound_event(
        self, event: DingTalkInboundEvent, *, history_group_id: str | None = None,
        history_sender_kind: str = "human", history_dispatch_after: datetime | None = None,
    ) -> InboundDisposition:
        from openjiuwen.harness.persistent_agent import DirectMessageSource, GroupMessage, RequestInput
        from jiuwenswarm.server.jiuwenmate.execution_models import (
            AccessSnapshot,
            ConversationRef,
            RequestRouteSnapshot,
        )
        from jiuwenswarm.server.jiuwenmate.group_collaboration import (
            greeting_respondent,
            is_pure_greeting,
        )
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
        from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_READ

        try:
            config_loader = self._config_loader
            if config_loader is None:
                from jiuwenswarm.common.config import get_config
                config_loader = get_config
            config = await asyncio.to_thread(config_loader)
            bindings = await asyncio.to_thread(configured_dingtalk_bindings, config)
            matches = [binding for binding in bindings
                       if binding.app_id == event.receiving_app_id and binding.agent_id == event.expected_agent_id]
            if len(matches) != 1:
                return InboundDisposition.REJECTED
            binding = matches[0]
            is_history = history_group_id is not None
            if event.robot_code is not None and event.robot_code != binding.robot_code:
                return InboundDisposition.REJECTED
            # Only enterprise-internal apps are supported, so a staffId from another
            # organization would name a different person in this robot's organization.
            if (
                event.sender_corp_id is not None and event.chatbot_corp_id is not None
                and event.sender_corp_id != event.chatbot_corp_id
            ):
                return InboundDisposition.REJECTED
            # The current YAML binding was checked above; only legacy identities need a connection row.
            connection = await asyncio.to_thread(self._service.connection_of, binding.namespace_id, binding.agent_id)
            if connection is None:
                await asyncio.to_thread(self._service.get_config, binding.namespace_id, binding.agent_id)
            elif (connection.channel, connection.connection_key) != ("dingtalk", binding.app_id):
                return InboundDisposition.REJECTED
            view = await asyncio.to_thread(self._service.get_colleague, binding.namespace_id, binding.agent_id)
            if getattr(view, "enabled", True) is False:
                return InboundDisposition.HANDLED
            snapshot = await asyncio.to_thread(self._service.current_snapshot, binding.namespace_id, binding.agent_id)
            received_at = event.received_at.astimezone(timezone.utc)
            is_group = event.conversation_type == "2"
            scope = frozenset({"chat", WORKSPACE_READ}) if is_group else frozenset(
                {"chat", "workspace_read", "workspace_knowledge_read"}
            )
            target_id = event.conversation_id if is_group else event.sender_staff_id
            policy_revision = "jm21-dingtalk-group-v1" if is_group else "jm21-dingtalk-direct-v1"
            access = AccessSnapshot(
                principal_ref=f"dingtalk:{binding.app_id}:{event.sender_staff_id}",
                startup_scope=scope, source_scope=scope, policy_revision=policy_revision,
            )
            if not is_history and await self._manager.handle_im_permission_reply(
                event.text, namespace_id=binding.namespace_id, agent_id=binding.agent_id,
                access=access, channel="dingtalk", target_id=target_id,
                app_id=binding.app_id, source_message_id=event.message_id,
                is_direct_message=not is_group,
            ):
                return InboundDisposition.HANDLED
            if event.conversation_type == "2":
                if is_history:
                    group = await asyncio.to_thread(
                        self._manager.group_chats.get, binding.namespace_id, history_group_id,
                    )
                    apps = await asyncio.to_thread(
                        self._manager.group_chats.active_apps,
                        binding.namespace_id, binding.agent_id, group.group_chat_id,
                    )
                    if (
                        group.platform != "dingtalk" or group.platform_chat_id != event.conversation_id
                        or binding.app_id not in apps
                    ):
                        return InboundDisposition.REJECTED
                    membership = await asyncio.to_thread(
                        self._manager.group_chats.access_for,
                        binding.namespace_id, binding.agent_id, group.group_chat_id,
                    )
                    if received_at < membership.visible_from:
                        return InboundDisposition.HANDLED
                    app = next((item for item in dingtalk_apps(config.get("channels", {}).get("dingtalk"))
                                if item["client_id"] == binding.app_id), None)
                    allowed = app.get("allow_from", []) if app is not None else []
                    # A staffId-only allowlist cannot be satisfied by guessing an open ID mapping.
                    if allowed and "*" not in allowed and event.sender_staff_id.removeprefix("dingtalk-open:") not in allowed:
                        return InboundDisposition.HANDLED
                else:
                    group = await asyncio.to_thread(
                        self._manager.group_chats.resolve, binding.namespace_id,
                        "dingtalk", event.conversation_id, now=received_at,
                    )
                    await asyncio.to_thread(
                        self._manager.group_chats.observe_access,
                        binding.namespace_id, binding.agent_id, group.group_chat_id, binding.app_id,
                        observed_at=received_at, source_ref=f"dingtalk:message:{event.message_id}",
                    )
                    membership = await asyncio.to_thread(
                        self._manager.group_chats.access_for,
                        binding.namespace_id, binding.agent_id, group.group_chat_id,
                    )
                    if membership.status != "active":
                        # Ingest time cannot prove freshness: retries may arrive after removal.
                        proof = event.platform_created_at
                        if proof is None or proof < received_at - timedelta(minutes=5):
                            return InboundDisposition.HANDLED
                        await asyncio.to_thread(
                            self._manager.group_chats.update_membership,
                            binding.namespace_id, binding.agent_id, group.group_chat_id, binding.app_id,
                            status="active", observed_at=proof,
                            source_ref=f"dingtalk:message:{event.message_id}",
                        )
                        membership = await asyncio.to_thread(
                            self._manager.group_chats.access_for,
                            binding.namespace_id, binding.agent_id, group.group_chat_id,
                        )
                        if membership.status != "active":
                            return InboundDisposition.HANDLED
                if event.group_name:
                    await asyncio.to_thread(
                        self._manager.group_chats.set_name,
                        binding.namespace_id,
                        group.group_chat_id,
                        event.group_name,
                    )
                source = GroupMessage(
                    message_id=event.message_id,
                    sender_id=event.sender_staff_id,
                    sender_name=event.sender_name,
                    sender_kind=history_sender_kind if is_history else "human",
                    received_at=received_at,
                    text=event.text,
                    mention_ids=event.mention_ids,
                    status="normal",
                )
                # The bot callback is not a complete stream of ordinary group traffic.
                # Keep this archive visibly partial even when this event is a duplicate.
                await asyncio.to_thread(
                    self._manager.group_archive.mark_gap,
                    binding.namespace_id,
                    group.group_chat_id,
                )
                try:
                    await asyncio.to_thread(
                        self._manager.group_archive.append,
                        binding.namespace_id,
                        group.group_chat_id,
                        source,
                        source_ref=f"dingtalk:message:{event.message_id}",
                        changed_at=None if is_history else received_at,
                    )
                except PersistentAgentError as exc:
                    if exc.code == "CONFLICT":
                        return InboundDisposition.HANDLED
                    raise

                if is_history and (
                    history_sender_kind != "human" or "@" in event.text or "＠" in event.text
                    or history_dispatch_after is None or received_at <= history_dispatch_after
                ):
                    return InboundDisposition.HANDLED

                if event.receiver_bot_mentioned:
                    source = source.model_copy(update={
                        "mention_ids": tuple(dict.fromkeys((*source.mention_ids, binding.agent_id))),
                    })
                else:
                    mentions_everyone = any(item.casefold() in {"all", "@_all"} for item in event.mention_ids)
                    if event.mention_ids and not mentions_everyone:
                        return InboundDisposition.HANDLED
                    settings = await asyncio.to_thread(
                        self._service.read_settings,
                        binding.namespace_id,
                        binding.agent_id,
                    )
                    if settings.group_reply_mode == "passive":
                        return InboundDisposition.HANDLED
                    if is_pure_greeting(event.text):
                        roster = await asyncio.to_thread(
                            self._manager.group_participant_roster,
                            binding.namespace_id,
                            group.group_chat_id,
                        )
                        enriched = []
                        for member in roster:
                            member_view = await asyncio.to_thread(
                                self._service.get_colleague,
                                binding.namespace_id,
                                member["agent_id"],
                            )
                            enriched.append({
                                **member,
                                "description": getattr(member_view, "description", ""),
                                "group_reply_mode": getattr(member_view, "group_reply_mode", "adaptive"),
                            })
                        respondent = greeting_respondent(enriched)
                        if respondent is not None and respondent != binding.agent_id:
                            return InboundDisposition.HANDLED
                conversation = ConversationRef.group(group.group_chat_id)
                request_input = RequestInput(
                    kind="group_message",
                    text=event.text,
                    source=source,
                    reply_target=event.conversation_id,
                )
            else:
                conversation = ConversationRef.im_direct(
                    "dingtalk", json.dumps([binding.app_id, event.conversation_id], separators=(",", ":")),
                )
                request_input = RequestInput(
                    kind="direct_message",
                    text=event.text,
                    source=DirectMessageSource(
                        message_id=event.message_id,
                        sender_id=event.sender_staff_id,
                        sender_name=event.sender_name,
                        received_at=received_at,
                    ),
                    reply_target=event.sender_staff_id,
                )
            session = await asyncio.to_thread(
                self._sessions.resolve_session, binding.namespace_id, binding.agent_id, conversation,
                now=received_at,
            )
            await self._manager.scheduler.accept(
                session, snapshot=snapshot,
                source_ref="dingtalk:message:" + json.dumps([binding.app_id, event.message_id], separators=(",", ":")),
                access=access,
                request_input=request_input,
                received_at=received_at,
                route_snapshot=RequestRouteSnapshot(
                    channel="dingtalk", app_id=binding.app_id, target_id=target_id,
                    source_message_id=event.message_id,
                ),
            )
        except Exception as exc:
            logger.warning("[DingTalk] JM request rejected: error_type=%s", type(exc).__name__)
            return InboundDisposition.REJECTED
        return InboundDisposition.HANDLED
