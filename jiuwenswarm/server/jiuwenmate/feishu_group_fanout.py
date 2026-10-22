"""Route bound Feishu events into the persistent Request path.

Binding and authorization data are deliberately supplied by the host. This
module does not persist credentials, infer permissions, or fall back to the
personal-agent route after a binding has claimed an event.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
    from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
    from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
    from openjiuwen.harness.persistent_agent import RequestInput

from jiuwenswarm.server.jiuwenmate.group_collaboration import (
    greeting_respondent,
    is_pure_greeting,
    waits_for_named_colleague,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class FeishuInboundEvent:
    """The minimum normalized Feishu facts needed to accept a Request."""

    receiving_app_id: str
    expected_agent_id: str | None = None
    message_id: str | None
    chat_id: str | None
    chat_type: str | None
    sender_type: str | None
    sender_id: str | None
    sender_name: str | None
    sender_app_id: str | None
    received_at: datetime | None
    text: str | None
    mention_ids: tuple[str, ...] = ()
    receiver_bot_mentioned: bool = False
    reply_to: str | None = None
    attachment_refs: tuple[str, ...] = ()
    event_kind: str = "message"
    event_id: str | None = None
    changed_at: datetime | None = None
    group_name: str | None = None
    # The delivery archive may already hold this platform id with the raw handoff token.
    allow_existing_message: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Return JSON-safe normalized facts for the internal Gateway E2A request."""
        return {
            "receiving_app_id": self.receiving_app_id,
            "expected_agent_id": self.expected_agent_id,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "sender_type": self.sender_type,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_app_id": self.sender_app_id,
            "received_at": self.received_at.isoformat() if isinstance(self.received_at, datetime) else None,
            "text": self.text,
            "mention_ids": list(self.mention_ids),
            "receiver_bot_mentioned": self.receiver_bot_mentioned,
            "reply_to": self.reply_to,
            "attachment_refs": list(self.attachment_refs),
            "event_kind": self.event_kind,
            "event_id": self.event_id,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
            "group_name": self.group_name,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> FeishuInboundEvent:
        """Parse the internal E2A event payload; later validation rejects bad facts."""
        if not isinstance(payload, Mapping):
            raise ValueError("JM-08 event payload must be a mapping")
        received_at = payload.get("received_at")
        if isinstance(received_at, str):
            try:
                received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            except ValueError:
                received_at = None
        elif not isinstance(received_at, datetime):
            received_at = None

        mention_ids = payload.get("mention_ids", ())
        if isinstance(mention_ids, list):
            mention_ids = tuple(mention_ids)
        attachment_refs = payload.get("attachment_refs", ())
        if isinstance(attachment_refs, list):
            attachment_refs = tuple(attachment_refs)

        changed_at = payload.get("changed_at")
        if isinstance(changed_at, str):
            changed_at = datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
        return cls(
            receiving_app_id=payload.get("receiving_app_id"),
            expected_agent_id=payload.get("expected_agent_id"),
            message_id=payload.get("message_id"),
            chat_id=payload.get("chat_id"),
            chat_type=payload.get("chat_type"),
            sender_type=payload.get("sender_type"),
            sender_id=payload.get("sender_id"),
            sender_name=payload.get("sender_name"),
            sender_app_id=payload.get("sender_app_id"),
            received_at=received_at,
            text=payload.get("text"),
            mention_ids=mention_ids,
            receiver_bot_mentioned=payload.get("receiver_bot_mentioned", False),
            reply_to=payload.get("reply_to"),
            attachment_refs=attachment_refs,
            event_kind=payload.get("event_kind", "message"), event_id=payload.get("event_id"),
            changed_at=changed_at, group_name=payload.get("group_name"),
        )


@dataclass(frozen=True, kw_only=True)
class FeishuBinding:
    """A host-resolved Feishu app binding to one persistent Agent."""

    binding_id: str
    platform: str
    app_id: str
    namespace_id: str
    agent_id: str


class InboundDisposition(str, Enum):
    """Whether the persistent path claimed, accepted, or rejected an event."""

    UNCLAIMED = "unclaimed"
    HANDLED = "handled"
    REJECTED = "rejected"


BindingResolver = Callable[[str, str], Sequence[FeishuBinding]]
AccessResolver = Callable[[FeishuBinding, FeishuInboundEvent], Any]


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def configured_feishu_bindings(
    config: Mapping[str, Any] | None = None,
) -> tuple[FeishuBinding, ...]:
    """Read JM app bindings from the owner's local Feishu configuration."""
    if config is None:
        from jiuwenswarm.common.config import get_config

        config = get_config()
    channels = config.get("channels") if isinstance(config, Mapping) else None
    if not isinstance(channels, Mapping):
        return ()

    bindings: list[FeishuBinding] = []

    def add_app(app: Any) -> None:
        if not isinstance(app, Mapping):
            return
        app_id = app.get("app_id")
        persistent_agent_id = app.get("persistent_agent_id")
        if app.get("enabled", True) is False:
            return
        if not _present(app_id) or not _present(persistent_agent_id):
            return
        normalized_app_id = app_id.strip()
        normalized_agent_id = persistent_agent_id.strip()
        bindings.append(
            FeishuBinding(
                binding_id=f"feishu:{normalized_app_id}",
                platform="feishu",
                app_id=normalized_app_id,
                namespace_id="local",
                agent_id=normalized_agent_id,
            )
        )

    feishu = channels.get("feishu")
    apps = feishu.get("apps") if isinstance(feishu, Mapping) else None
    if isinstance(apps, list):
        for app in apps:
            add_app(app)

    enterprise = channels.get("feishu_enterprise")
    if isinstance(enterprise, Mapping):
        for app in enterprise.values():
            add_app(app)
    app_counts: dict[str, int] = {}
    agent_counts: dict[str, int] = {}
    for item in bindings:
        app_counts[item.app_id] = app_counts.get(item.app_id, 0) + 1
        agent_counts[item.agent_id] = agent_counts.get(item.agent_id, 0) + 1
    return tuple(
        sorted(
            (item for item in bindings if app_counts[item.app_id] == 1 and agent_counts[item.agent_id] == 1),
            key=lambda item: (item.app_id, item.agent_id),
        )
    )


def resolve_configured_feishu_bindings(platform: str, app_id: str) -> tuple[FeishuBinding, ...]:
    """Resolve the unique local Agent binding for a receiving Feishu app ID."""
    if platform != "feishu" or not _present(app_id):
        return ()
    return tuple(item for item in configured_feishu_bindings() if item.app_id == app_id.strip())


def resolve_feishu_access(
    _binding: FeishuBinding, event: FeishuInboundEvent
) -> AccessSnapshot:
    """Record the Feishu principal; grant workspace reads to both chat types and knowledge reads to direct messages."""
    from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot
    from jiuwenswarm.server.jiuwenmate.ports.workspace_access import (
        WORKSPACE_KNOWLEDGE_READ,
        WORKSPACE_READ,
    )

    sender_type = str(event.sender_type or "unknown").strip().lower()
    sender_id = event.sender_app_id if sender_type in {"bot", "app", "agent"} and _present(event.sender_app_id) else event.sender_id
    scope = {"chat", WORKSPACE_READ}
    if event.chat_type == "p2p":
        scope.add(WORKSPACE_KNOWLEDGE_READ)
    return AccessSnapshot(
        principal_ref=f"feishu:{sender_type}:{sender_id}",
        startup_scope=frozenset(scope),
        source_scope=frozenset(scope),
        policy_revision="jm08-feishu-v2",
    )


def create_default_persistent_group_fanout(
    data_dir: Path | None = None,
    *,
    send_delivery: Callable[[dict[str, object]], Awaitable[Mapping[str, Any] | None]] | None = None,
    thinking_notice_sender: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    verify_membership=None,
) -> tuple[PersistentGroupFanout, AgentManager]:
    """Build JM-08 on the owner's private JiuwenMate SQLite store and JM-07 manager."""
    from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
    from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
    from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
    from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService, default_data_dir
    from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
    from jiuwenswarm.server.jiuwenmate.conversation_resources import ConversationResources

    root = data_dir if data_dir is not None else default_data_dir()
    db_path = root / "agents.sqlite3"
    service = PersistentAgentService.create_default(root)
    sessions = ExecutionRepository(db_path)
    groups = GroupChatRepository(db_path)
    windows = WindowBackendStore(db_path)
    budgets = BudgetRepository(db_path)
    resources = ConversationResources(ConversationArchive(root / "group_messages", groups), sessions)

    async def archive_delivery(job):
        await asyncio.to_thread(resources.archive_delivery, job)

    delivery_service = (
        ResultDeliveryService(sessions, namespace_id="local", send_delivery=send_delivery, archive_delivery=archive_delivery)
        if send_delivery is not None
        else None
    )
    manager = AgentManager(
        service,
        sessions,
        windows,
        budgets,
        delivery_service=delivery_service,
        thinking_notice_sender=thinking_notice_sender,
    )
    if verify_membership is not None:
        async def verify(access, group):
            bindings = resolve_configured_feishu_bindings("feishu", access.app_id)
            if len(bindings) != 1 or (bindings[0].namespace_id, bindings[0].agent_id) != (
                    access.namespace_id, access.agent_id):
                return "unknown"
            facts = await verify_membership(access.app_id, group.platform_chat_id)
            status = facts.get("status", "unknown")
            if status == "left":
                await asyncio.to_thread(groups.update_membership, access.namespace_id, access.agent_id,
                                       access.group_id, access.app_id, status="left",
                                       observed_at=datetime.now(timezone.utc), source_ref="membership:live")
                return "left"
            if status == "active" and facts.get("group_name"):
                await asyncio.to_thread(groups.set_name, access.namespace_id, access.group_id, facts["group_name"])
            if status == "active":
                return "active"
            # Feishu sometimes does not answer during a reconnect. That is not proof the Bot left.
            if access.status == "active":
                logger.warning(
                    "[JiuwenMate] group membership check inconclusive, keeping the active binding: app_id=%s",
                    access.app_id,
                )
                return "active"
            return "unknown"
        manager.verify_group_membership = verify

        async def refresh(access, group, messages):
            facts = await verify_membership(access.app_id, group.platform_chat_id,
                                           tuple(item.message_id for item in messages))
            if facts.get("status") == "unknown":
                # A slow or dropped Feishu read is not proof the archived text changed.
                logger.warning(
                    "[JiuwenMate] group message refresh got no Feishu answer, keeping the archived text: agent_id=%s",
                    access.agent_id,
                )
                return tuple(messages)
            if facts.get("status") != "ok" or not isinstance(facts.get("messages"), dict):
                raise ValueError("current group message state unavailable")
            updated = []
            for message in messages:
                item = facts["messages"].get(message.message_id, {})
                status = item.get("status")
                if status == "recalled":
                    await asyncio.to_thread(manager.group_archive.recall, access.namespace_id, access.group_id,
                        message.message_id, source_ref=f"refresh:{message.message_id}", changed_at=datetime.now(timezone.utc))
                    updated.append(message.model_copy(update={"status": "recalled", "text": None, "attachment_refs": ()}))
                elif status in {"normal", "edited"}:
                    changed_at = datetime.fromisoformat(item["changed_at"])
                    fresh = type(message).model_validate({**message.model_dump(), "status": status,
                        "text": item.get("text"), "attachment_refs": item.get("attachment_refs", ())})
                    await asyncio.to_thread(
                        manager.group_archive.append, access.namespace_id, access.group_id, fresh,
                        source_ref=f"refresh:{message.message_id}", changed_at=changed_at,
                        allow_resolved_mentions=True,
                    )
                    updated.append(fresh)
                elif status == "unknown":
                    # Feishu did not answer. The archived inbound text remains the trigger.
                    updated.append(message)
                else:
                    updated.append(message.model_copy(update={"status": "unavailable", "text": None, "attachment_refs": ()}))
            return tuple(updated)

        manager.refresh_group_messages = refresh
    fanout = PersistentGroupFanout(
        service=service,
        groups=groups,
        sessions=sessions,
        agent_manager=manager,
        resolve_bindings=resolve_configured_feishu_bindings,
        resolve_access=resolve_feishu_access,
    )
    if delivery_service is not None:
        delivery_service.set_handoff_handler(fanout.continue_handoff)
    return fanout, manager


def _valid_received_at(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


class PersistentGroupFanout:
    """Resolve one receiving-app binding and durably accept its Feishu event.

    The resolver and authorization provider may be synchronous or asynchronous.
    SQLite repository calls and the synchronous configuration lookup run in a
    worker thread; the already-started AgentManager scheduler is awaited on the
    event loop.
    """

    def __init__(
        self,
        *,
        service: PersistentAgentService,
        groups: GroupChatRepository,
        sessions: ExecutionRepository,
        agent_manager: AgentManager,
        resolve_bindings: BindingResolver,
        resolve_access: AccessResolver,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._service = service
        self._groups = groups
        self._sessions = sessions
        self._agent_manager = agent_manager
        self._resolve_bindings = resolve_bindings
        self._resolve_access = resolve_access
        self._clock = clock

    async def handle(self, event: FeishuInboundEvent) -> InboundDisposition:
        """Claim the event only when exactly one configured app binding matches."""
        if not isinstance(event, FeishuInboundEvent) or not _present(event.receiving_app_id):
            return InboundDisposition.REJECTED

        try:
            bindings = await self._call(self._resolve_bindings, "feishu", event.receiving_app_id)
            if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes)):
                return InboundDisposition.REJECTED
            matches = tuple(bindings)
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] Feishu binding lookup failed closed: error_type=%s",
                type(exc).__name__,
            )
            return InboundDisposition.REJECTED

        if not matches:
            return (
                InboundDisposition.REJECTED
                if _present(event.expected_agent_id)
                else InboundDisposition.UNCLAIMED
            )
        if len(matches) != 1 or not isinstance(matches[0], FeishuBinding):
            return InboundDisposition.REJECTED

        binding = matches[0]
        if (
            binding.platform != "feishu"
            or binding.app_id != event.receiving_app_id
            or not all(
                _present(value)
                for value in (binding.binding_id, binding.namespace_id, binding.agent_id)
            )
        ):
            return InboundDisposition.REJECTED
        if _present(event.expected_agent_id) and binding.agent_id != event.expected_agent_id.strip():
            return InboundDisposition.REJECTED

        if event.event_kind != "message":
            return await self._handle_group_change(binding, event)
        sender_kind = self._validated_sender_kind(event)
        if sender_kind is None:
            return InboundDisposition.REJECTED
        if not await self._im_open(binding):
            return InboundDisposition.HANDLED

        try:
            # Keep ordinary personal-mode gateway startup independent of the
            # persistent runtime's heavier core imports. These contracts are
            # loaded only after a binding has claimed a valid event.
            from jiuwenswarm.server.jiuwenmate.execution_models import (
                AccessSnapshot,
                ConversationRef,
                RequestRouteSnapshot,
            )

            snapshot = await asyncio.to_thread(
                self._service.current_snapshot,
                binding.namespace_id,
                binding.agent_id,
            )
            if snapshot.agent_id != binding.agent_id:
                return InboundDisposition.REJECTED
            access = await self._call(self._resolve_access, binding, event)
            if not isinstance(access, AccessSnapshot):
                return InboundDisposition.REJECTED

            # Only a platform-supplied sender application identity can prove
            # that this is the receiving Bot echoing its own message.
            sender_app_id = event.sender_app_id.strip() if _present(event.sender_app_id) else None
            request_input = self._request_input(event, sender_kind)
            received_at = event.received_at.astimezone(timezone.utc)
            now = self._clock()
            if not _valid_received_at(now):
                return InboundDisposition.REJECTED

            if event.chat_type == "group":
                group = await asyncio.to_thread(
                    self._groups.resolve,
                    binding.namespace_id,
                    "feishu",
                    event.chat_id,
                    now=now,
                )
                conversation = ConversationRef.group(group.group_chat_id)
                apps = await asyncio.to_thread(self._groups.active_apps, binding.namespace_id,
                                               binding.agent_id, group.group_chat_id)
                for app_id in apps:
                    if app_id == binding.app_id:
                        continue
                    previous = await self._call(self._resolve_bindings, "feishu", app_id)
                    if not any((item.namespace_id, item.agent_id) == (binding.namespace_id, binding.agent_id)
                               for item in previous):
                        await asyncio.to_thread(self._groups.update_membership, binding.namespace_id,
                            binding.agent_id, group.group_chat_id, app_id, status="unknown", observed_at=now,
                            source_ref=f"binding:{binding.binding_id}")
                await asyncio.to_thread(
                    self._groups.observe_access, binding.namespace_id, binding.agent_id, group.group_chat_id,
                    binding.app_id, observed_at=received_at, source_ref=f"feishu:message:{event.message_id}",
                )
                from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

                presented_text = request_input.text
                archived = False
                try:
                    await asyncio.to_thread(
                        self._agent_manager.group_archive.append, binding.namespace_id, group.group_chat_id,
                        request_input.source, source_ref=f"feishu:message:{event.message_id}", changed_at=event.changed_at,
                    )
                    archived = True
                except PersistentAgentError as exc:
                    # Delivery reconciliation stores the raw token under this id before the handoff runs.
                    if not event.allow_existing_message or exc.code != "CONFLICT":
                        raise
                current, = await asyncio.to_thread(self._agent_manager.group_archive.current_messages,
                    binding.namespace_id, group.group_chat_id, (event.message_id,))
                if current.status in {"recalled", "unavailable"}:
                    return InboundDisposition.HANDLED
                source = current
                text = current.text or ("[附件消息]" if current.attachment_refs else "[群消息正文为空]")
                if event.allow_existing_message and not archived and isinstance(presented_text, str) and presented_text:
                    source = current.model_copy(update={"text": presented_text})
                    text = presented_text
                request_input = request_input.model_copy(update={"source": source,
                    "text": text,
                    "attachment_refs": current.attachment_refs})
                if event.receiver_bot_mentioned:
                    # Keep the shared archive's Feishu open_id intact. PA-07 consumes the
                    # receiving Agent's internal ID only on this request-specific copy.
                    request_source = request_input.source.model_copy(
                        update={
                            "mention_ids": tuple(
                                dict.fromkeys((*request_input.source.mention_ids, binding.agent_id))
                            )
                        }
                    )
                    request_input = request_input.model_copy(update={"source": request_source})
                else:
                    settings = await asyncio.to_thread(
                        self._service.read_settings, binding.namespace_id, binding.agent_id,
                    )
                    if settings.group_reply_mode == "passive":
                        return InboundDisposition.HANDLED
                    roster = await asyncio.to_thread(
                        self._agent_manager.group_participant_roster,
                        binding.namespace_id,
                        group.group_chat_id,
                    )
                    if is_pure_greeting(request_input.text or ""):
                        enriched = []
                        for member in roster:
                            view = await asyncio.to_thread(
                                self._service.get_colleague, binding.namespace_id, member["agent_id"],
                            )
                            enriched.append({
                                **member,
                                "description": getattr(view, "description", ""),
                                "group_reply_mode": getattr(view, "group_reply_mode", "adaptive"),
                            })
                        respondent = greeting_respondent(enriched)
                        if respondent is not None and respondent != binding.agent_id:
                            logger.info(
                                "[JiuwenMate] group greeting is answered by another colleague: agent_id=%s",
                                binding.agent_id,
                            )
                            return InboundDisposition.HANDLED
                    elif waits_for_named_colleague(request_input.text or "", roster, binding.agent_id):
                        logger.info(
                            "[JiuwenMate] group message waits for the named colleague: agent_id=%s",
                            binding.agent_id,
                        )
                        return InboundDisposition.HANDLED
                if event.group_name:
                    await asyncio.to_thread(self._groups.set_name, binding.namespace_id, group.group_chat_id, event.group_name)
            else:
                conversation = ConversationRef.im_direct("feishu", event.chat_id)

            if sender_kind == "agent" and sender_app_id == event.receiving_app_id:
                return InboundDisposition.HANDLED

            session = await asyncio.to_thread(
                self._sessions.resolve_session,
                binding.namespace_id,
                binding.agent_id,
                conversation,
                now=now,
            )
            source_ref = f"feishu:message:{event.message_id}"
            await self._agent_manager.scheduler.accept(
                session,
                snapshot=snapshot,
                source_ref=source_ref,
                access=access,
                request_input=request_input,
                received_at=received_at,
                route_snapshot=RequestRouteSnapshot(
                    channel="feishu",
                    app_id=event.receiving_app_id,
                    target_id=event.chat_id,
                    source_message_id=event.message_id,
                    source_reply_to=event.reply_to,
                ),
            )
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] Feishu event rejected: app_id=%s error_type=%s error=%s",
                event.receiving_app_id,
                type(exc).__name__,
                exc,
            )
            return InboundDisposition.REJECTED

        return InboundDisposition.HANDLED

    async def _im_open(self, binding: FeishuBinding) -> bool:
        """A disabled colleague claims the event and does not start a Request."""
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

        try:
            view = await asyncio.to_thread(self._service.get_colleague, binding.namespace_id, binding.agent_id)
        except PersistentAgentError:
            return False
        return getattr(view, "enabled", True) is not False

    async def _handle_group_change(self, binding: FeishuBinding, event: FeishuInboundEvent) -> InboundDisposition:
        """Lifecycle and recall facts change host records, never create business Requests."""
        if (event.event_kind not in {"recalled", "joined", "left", "group_updated"}
                or event.chat_type != "group" or not _present(event.chat_id)
                or not _present(event.event_id) or not _valid_received_at(event.changed_at)):
            return InboundDisposition.REJECTED
        try:
            group = await asyncio.to_thread(self._groups.resolve, binding.namespace_id, "feishu", event.chat_id,
                                             now=self._clock())
            source = f"feishu:event:{event.event_id}"
            if event.event_kind == "recalled":
                if not _present(event.message_id):
                    return InboundDisposition.REJECTED
                await asyncio.to_thread(self._agent_manager.group_archive.recall, binding.namespace_id,
                                         group.group_chat_id, event.message_id, source_ref=source,
                                         changed_at=event.changed_at)
            elif event.event_kind in {"joined", "left"}:
                await asyncio.to_thread(self._groups.update_membership, binding.namespace_id, binding.agent_id,
                                         group.group_chat_id, binding.app_id,
                                         status="active" if event.event_kind == "joined" else "left",
                                         observed_at=event.changed_at, source_ref=source)
            if event.group_name:
                await asyncio.to_thread(self._groups.set_name, binding.namespace_id, group.group_chat_id, event.group_name)
        except Exception:
            return InboundDisposition.REJECTED
        return InboundDisposition.HANDLED

    async def continue_handoff(
        self,
        job: Mapping[str, object],
        mentions: Sequence[Mapping[str, Any]],
        external_message_id: str | None,
    ) -> None:
        """Start the named colleague from a delivered public reply.

        Feishu does not push that Bot reply back as a new inbound event. The
        target still enters through the same group Request path, keyed by the
        platform message id so a later callback does not start a second one.
        """
        if job.get("is_direct_message") is True or len(mentions) != 1:
            return
        if not isinstance(external_message_id, str) or not external_message_id.strip():
            return
        target = mentions[0]
        agent_id = target.get("agent_id")
        name = target.get("name")
        app_ids = target.get("app_ids")
        sender_id = job.get("agent_id")
        namespace_id = job.get("namespace_id")
        chat_id = job.get("target_id")
        sender_app_id = job.get("app_id")
        if (
            not isinstance(agent_id, str)
            or not isinstance(name, str)
            or not isinstance(app_ids, list)
            or len(app_ids) != 1
            or not isinstance(app_ids[0], str)
            or not isinstance(sender_id, str)
            or not isinstance(namespace_id, str)
            or not isinstance(chat_id, str)
            or not isinstance(sender_app_id, str)
        ):
            return
        colleague = await asyncio.to_thread(self._service.get_colleague, namespace_id, sender_id)
        reply_to = job.get("source_message_id")
        text = str(job.get("text") or "").replace(f"[[agent:{agent_id}]]", f"@{name}")
        event = FeishuInboundEvent(
            receiving_app_id=app_ids[0],
            expected_agent_id=agent_id,
            message_id=external_message_id.strip(),
            chat_id=chat_id,
            chat_type="group",
            sender_type="bot",
            sender_id=sender_id,
            sender_name=colleague.name,
            sender_app_id=sender_app_id,
            received_at=self._clock(),
            text=text,
            receiver_bot_mentioned=True,
            reply_to=reply_to if isinstance(reply_to, str) and reply_to.strip() else None,
            allow_existing_message=True,
        )
        await self.handle(event)

    async def _call(self, callback: Callable[..., Any], *args: Any) -> Any:
        result = await asyncio.to_thread(callback, *args)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _validated_sender_kind(event: FeishuInboundEvent) -> str | None:
        if (
            not _present(event.message_id)
            or not _present(event.chat_id)
            or not _valid_received_at(event.received_at)
            or not _present(event.sender_id)
            or not _present(event.sender_name)
            or not isinstance(event.text, (str, type(None)))
            or not isinstance(event.mention_ids, tuple)
            or any(not _present(item) for item in event.mention_ids)
            or not isinstance(event.receiver_bot_mentioned, bool)
            or not isinstance(event.attachment_refs, tuple)
            or any(not _present(item) for item in event.attachment_refs)
            or (event.reply_to is not None and not _present(event.reply_to))
        ):
            return None

        if event.chat_type not in ("group", "p2p"):
            return None
        sender_type = event.sender_type.strip().lower() if _present(event.sender_type) else ""
        if sender_type in ("user", "human"):
            return "human"
        if sender_type in ("bot", "app", "agent"):
            return "agent"
        return None

    @staticmethod
    def _request_input(event: FeishuInboundEvent, sender_kind: str) -> RequestInput:
        from openjiuwen.harness.persistent_agent import DirectMessageSource, GroupMessage, RequestInput

        attachment_refs = tuple(event.attachment_refs)
        source_text = event.text
        request_text = source_text if _present(source_text) else "[附件消息]" if attachment_refs else None
        if request_text is None:
            raise ValueError("Feishu message has neither text nor attachment references")

        if event.chat_type == "group":
            source = GroupMessage(
                message_id=event.message_id,
                sender_id=event.sender_id,
                sender_name=event.sender_name,
                sender_kind=sender_kind,
                received_at=event.received_at,
                text=source_text,
                mention_ids=event.mention_ids,
                reply_to=event.reply_to,
                attachment_refs=attachment_refs,
                status="edited" if event.changed_at is not None else "normal",
            )
            kind = "group_message"
        else:
            source = DirectMessageSource(
                message_id=event.message_id,
                sender_id=event.sender_id,
                sender_name=event.sender_name,
                received_at=event.received_at,
            )
            kind = "direct_message"

        return RequestInput(
            kind=kind,
            text=request_text,
            source=source,
            reply_target=event.chat_id,
            attachment_refs=attachment_refs,
        )
