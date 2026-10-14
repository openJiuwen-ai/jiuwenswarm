"""Route bound Feishu events into the persistent Request path.

Binding and authorization data are deliberately supplied by the host. This
module does not persist credentials, infer permissions, or fall back to the
personal-agent route after a binding has claimed an event.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
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
    reply_to: str | None = None
    attachment_refs: tuple[str, ...] = ()

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
            "reply_to": self.reply_to,
            "attachment_refs": list(self.attachment_refs),
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
            reply_to=payload.get("reply_to"),
            attachment_refs=attachment_refs,
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
        agent_id = app.get("agent_id")
        if not _present(app_id) or not _present(agent_id):
            return
        normalized_app_id = app_id.strip()
        bindings.append(
            FeishuBinding(
                binding_id=f"feishu:{normalized_app_id}",
                platform="feishu",
                app_id=normalized_app_id,
                namespace_id="local",
                agent_id=agent_id.strip(),
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
    return tuple(sorted(bindings, key=lambda item: (item.app_id, item.agent_id)))


def resolve_configured_feishu_bindings(platform: str, app_id: str) -> tuple[FeishuBinding, ...]:
    """Resolve the unique local Agent binding for a receiving Feishu app ID."""
    if platform != "feishu" or not _present(app_id):
        return ()
    return tuple(item for item in configured_feishu_bindings() if item.app_id == app_id.strip())


def resolve_feishu_access(
    _binding: FeishuBinding, event: FeishuInboundEvent
) -> AccessSnapshot:
    """Record the Feishu principal without granting workspace scopes."""
    from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot

    sender_type = str(event.sender_type or "unknown").strip().lower()
    sender_id = event.sender_app_id if sender_type in {"bot", "app", "agent"} and _present(event.sender_app_id) else event.sender_id
    return AccessSnapshot(
        principal_ref=f"feishu:{sender_type}:{sender_id}",
        startup_scope=frozenset(),
        source_scope=frozenset(),
        policy_revision="jm08-feishu-v1",
    )


def create_default_persistent_group_fanout(
    data_dir: Path | None = None,
) -> tuple[PersistentGroupFanout, AgentManager]:
    """Build JM-08 on the owner's private JiuwenMate SQLite store and JM-07 manager."""
    from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
    from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
    from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
    from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService, default_data_dir

    root = data_dir if data_dir is not None else default_data_dir()
    db_path = root / "agents.sqlite3"
    service = PersistentAgentService.create_default(root)
    sessions = ExecutionRepository(db_path)
    groups = GroupChatRepository(db_path)
    windows = WindowBackendStore(db_path)
    budgets = BudgetRepository(db_path)
    manager = AgentManager(service, sessions, windows, budgets)
    fanout = PersistentGroupFanout(
        service=service,
        groups=groups,
        sessions=sessions,
        agent_manager=manager,
        resolve_bindings=resolve_configured_feishu_bindings,
        resolve_access=resolve_feishu_access,
    )
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

        sender_kind = self._validated_sender_kind(event)
        if sender_kind is None:
            return InboundDisposition.REJECTED

        try:
            # Keep ordinary personal-mode gateway startup independent of the
            # persistent runtime's heavier core imports. These contracts are
            # loaded only after a binding has claimed a valid event.
            from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef

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
            if sender_kind == "agent" and sender_app_id == event.receiving_app_id:
                return InboundDisposition.HANDLED

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
            else:
                conversation = ConversationRef.im_direct("feishu", event.chat_id)

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
            )
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] Feishu event rejected: app_id=%s error_type=%s",
                event.receiving_app_id,
                type(exc).__name__,
            )
            return InboundDisposition.REJECTED

        return InboundDisposition.HANDLED

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
