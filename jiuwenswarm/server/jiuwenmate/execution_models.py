"""Host records that tie one Request and its Executions to core invocation values.

The protocol types themselves (``RequestInput``, ``ExecutionContext``) come from
agent-core; this module only stores the host facts needed to rebuild them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ValidationError

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.harness.persistent_agent import ExecutionContext, RequestInput


class PersistentContractError(ValueError):
    """Host data cannot be converted into a valid agent-core invocation."""

    def __init__(self, message: str, *, code: str = "PERSISTENT_CONTRACT_INVALID") -> None:
        super().__init__(message)
        self.code = code


def _require_text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PersistentContractError(f"missing {name}")


def _scope(name: str, values: Any) -> frozenset[str]:
    if not isinstance(values, (set, frozenset, list, tuple)):
        raise PersistentContractError(f"invalid {name}")
    return frozenset(values)


@dataclass(frozen=True, kw_only=True)
class AccessSnapshot:
    """Initiator and permission facts fixed when the Request was accepted."""

    principal_ref: str
    startup_scope: frozenset[str]
    source_scope: frozenset[str]
    policy_revision: str

    def __post_init__(self) -> None:
        _require_text("principal_ref", self.principal_ref)
        _require_text("policy_revision", self.policy_revision)
        object.__setattr__(self, "startup_scope", _scope("startup_scope", self.startup_scope))
        object.__setattr__(self, "source_scope", _scope("source_scope", self.source_scope))

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_ref": self.principal_ref,
            "startup_scope": sorted(self.startup_scope),
            "source_scope": sorted(self.source_scope),
            "policy_revision": self.policy_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccessSnapshot:
        return cls(**data)


@dataclass(frozen=True, kw_only=True)
class RequestRecord:
    """One accepted task, persisted before dispatch and reused by every Execution."""

    namespace_id: str
    agent_id: str
    session_id: str
    request_id: str
    source_ref: str
    config_version: str
    config_fingerprint: str
    access: AccessSnapshot
    request_input: RequestInput
    received_at: datetime
    route_snapshot: RequestRouteSnapshot | None = None

    def __post_init__(self) -> None:
        for name in (
            "namespace_id",
            "agent_id",
            "session_id",
            "request_id",
            "source_ref",
            "config_version",
            "config_fingerprint",
        ):
            _require_text(name, getattr(self, name))
        if not isinstance(self.access, AccessSnapshot):
            raise PersistentContractError("invalid access snapshot")
        try:
            request_input = RequestInput.model_validate(self.request_input)
        except ValidationError as exc:
            raise PersistentContractError(f"invalid RequestInput: {exc}") from exc
        if request_input.continue_instruction is not None:
            raise PersistentContractError("a new Request cannot carry continue_instruction")
        object.__setattr__(self, "request_input", request_input)
        if not isinstance(self.received_at, datetime) or self.received_at.tzinfo is None:
            raise PersistentContractError("received_at must be timezone-aware")
        object.__setattr__(self, "received_at", self.received_at.astimezone(timezone.utc))
        if self.route_snapshot is not None and not isinstance(self.route_snapshot, RequestRouteSnapshot):
            raise PersistentContractError("invalid Request route snapshot")

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace_id": self.namespace_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "source_ref": self.source_ref,
            "config_version": self.config_version,
            "config_fingerprint": self.config_fingerprint,
            "access": self.access.to_dict(),
            "request_input": self.request_input.model_dump(mode="json"),
            "received_at": self.received_at.isoformat(),
            "route_snapshot": None if self.route_snapshot is None else self.route_snapshot.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequestRecord:
        fields = dict(data)
        fields["access"] = AccessSnapshot.from_dict(fields["access"])
        fields["received_at"] = datetime.fromisoformat(fields["received_at"])
        route = fields.get("route_snapshot")
        fields["route_snapshot"] = None if route is None else RequestRouteSnapshot.from_dict(route)
        return cls(**fields)


@dataclass(frozen=True, kw_only=True)
class RequestRouteSnapshot:
    """The trusted delivery destination captured when a Request was accepted."""

    channel: Literal["web", "feishu", "dingtalk", "welink"]
    target_id: str
    app_id: str | None = None
    source_message_id: str | None = None
    source_reply_to: str | None = None

    def __post_init__(self) -> None:
        if self.channel not in ("web", "feishu", "dingtalk", "welink"):
            raise PersistentContractError("invalid Request route channel")
        _require_text("route target_id", self.target_id)
        for name in ("app_id", "source_message_id", "source_reply_to"):
            value = getattr(self, name)
            if value is not None:
                _require_text(f"route {name}", value)
        if self.channel in ("feishu", "dingtalk", "welink") and (self.app_id is None or self.source_message_id is None):
            raise PersistentContractError("an IM route requires app_id and source_message_id")
        if self.channel == "web" and self.app_id is not None:
            raise PersistentContractError("a Web route cannot name a Feishu app_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "app_id": self.app_id,
            "target_id": self.target_id,
            "source_message_id": self.source_message_id,
            "source_reply_to": self.source_reply_to,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequestRouteSnapshot:
        if not isinstance(data, dict):
            raise PersistentContractError("invalid Request route snapshot")
        return cls(**data)


@dataclass(frozen=True, kw_only=True)
class ExecutionRecord:
    """One attempt of a Request; a continuation names the earlier attempt it resumes."""

    request_id: str
    execution_id: str
    recovery_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text("request_id", self.request_id)
        _require_text("execution_id", self.execution_id)
        if self.recovery_ref is not None:
            _require_text("recovery_ref", self.recovery_ref)
            if self.recovery_ref == self.execution_id:
                raise PersistentContractError("recovery_ref must name an earlier Execution")

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "execution_id": self.execution_id, "recovery_ref": self.recovery_ref}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionRecord:
        return cls(**data)


ConversationKind = Literal["web_direct", "im_direct", "group"]
_REQUEST_KINDS: dict[str, frozenset[str]] = {
    "web_direct": frozenset({"direct_message", "scheduled"}),
    "im_direct": frozenset({"direct_message", "scheduled"}),
    "group": frozenset({"group_message", "scheduled"}),
}
RequestStatus = Literal["queued", "dispatched", "ended"]
ENDED_OUTCOMES = frozenset({"completed", "ignored", "paused", "failed", "cancelled", "stopped"})


@dataclass(frozen=True, kw_only=True)
class ConversationRef:
    """Where a colleague talks: one Web conversation, one IM private chat or one Swarm group.

    ``channel`` is ``web`` for Web, the IM platform for private chats, and ``group``
    for groups, whose ``key`` is the Swarm ``group_chat_id``. Every part is stored
    whole; no part is ever truncated or joined into a single string.
    """

    kind: ConversationKind
    channel: str
    key: str

    def __post_init__(self) -> None:
        if self.kind not in _REQUEST_KINDS:
            raise PersistentContractError("invalid conversation kind")
        _require_text("conversation channel", self.channel)
        _require_text("conversation key", self.key)
        if self.kind == "web_direct" and self.channel != "web":
            raise PersistentContractError("a Web conversation uses the web channel")
        if self.kind == "group" and self.channel != "group":
            raise PersistentContractError("a group conversation is keyed by group_chat_id")
        if self.kind == "im_direct" and self.channel in ("web", "group"):
            raise PersistentContractError("an IM private chat names its platform")

    @classmethod
    def web(cls, conversation_id: str) -> ConversationRef:
        return cls(kind="web_direct", channel="web", key=conversation_id)

    @classmethod
    def im_direct(cls, platform: str, chat_id: str) -> ConversationRef:
        return cls(kind="im_direct", channel=platform, key=chat_id)

    @classmethod
    def group(cls, group_chat_id: str) -> ConversationRef:
        return cls(kind="group", channel="group", key=group_chat_id)

    def accepts(self, request_kind: str) -> bool:
        return request_kind in _REQUEST_KINDS[self.kind]


@dataclass(frozen=True, kw_only=True)
class SessionRecord:
    """The stable Session of one colleague in one conversation."""

    namespace_id: str
    agent_id: str
    session_id: str
    conversation: ConversationRef
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class RequestState:
    """A stored Request plus its place in the Session queue and its dispatch state."""

    record: RequestRecord
    seq: int
    status: RequestStatus
    outcome: str | None
    updated_at: datetime


def build_execution_context(
    request: RequestRecord,
    execution: ExecutionRecord,
    *,
    capability_snapshot: frozenset[str],
) -> ExecutionContext:
    """Rebuild the immutable core identity of one Execution from host records."""
    if execution.request_id != request.request_id:
        raise PersistentContractError("Execution belongs to another Request")
    startup_scope = request.access.startup_scope
    source_scope = request.access.source_scope
    if (
        request.request_input.kind == "direct_message"
        and request.access.principal_ref.startswith("feishu:")
        and request.access.policy_revision == "jm08-feishu-v1"
        and "chat" in startup_scope
        and "chat" in source_scope
    ):
        # Recover only the direct-message permission for Requests persisted before JM-08-v2.
        startup_scope = startup_scope | frozenset({"workspace_knowledge_read"})
        source_scope = source_scope | frozenset({"workspace_knowledge_read"})
    try:
        return ExecutionContext(
            namespace_id=request.namespace_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            request_id=request.request_id,
            execution_id=execution.execution_id,
            source_ref=request.source_ref,
            config_version=request.config_version,
            config_fingerprint=request.config_fingerprint,
            principal_ref=request.access.principal_ref,
            startup_scope=startup_scope,
            source_scope=source_scope,
            policy_revision=request.access.policy_revision,
            capability_snapshot=capability_snapshot,
            recovery_ref=execution.recovery_ref,
        )
    except BaseError as exc:
        raise PersistentContractError(f"invalid ExecutionContext: {exc}") from exc
