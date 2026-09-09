"""Domain models for A2A outbound discovery, registration, and dispatch."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from .credentials import A2AOutboundCredentialStore
from .errors import A2AOutboundError, A2AOutboundErrorCode


class A2AOutboundAvailability(str, Enum):
    AVAILABLE = "available"
    UNREACHABLE = "unreachable"
    INCOMPATIBLE = "incompatible"
    REVIEW_REQUIRED = "review_required"


class A2AOutboundDispatchMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class A2AOutboundDispatchStatus(str, Enum):
    CREATED = "created"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"
    UNKNOWN = "unknown"
    TIMED_OUT = "timed_out"
    DISPATCH_FAILED = "dispatch_failed"


TERMINAL_DISPATCH_STATUSES: frozenset[A2AOutboundDispatchStatus] = frozenset(
    {
        A2AOutboundDispatchStatus.COMPLETED,
        A2AOutboundDispatchStatus.FAILED,
        A2AOutboundDispatchStatus.CANCELED,
        A2AOutboundDispatchStatus.REJECTED,
        A2AOutboundDispatchStatus.DISPATCH_FAILED,
    }
)

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "credential",
        "credentials",
    }
)
_REDACTED = "******"
_SENSITIVE_KEY_COMPACTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "apikey",
    "credential",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    compact = normalized.replace("_", "")
    return (
        normalized in _SENSITIVE_KEYS
        or compact.startswith(_SENSITIVE_KEY_COMPACTS)
        or compact.endswith(_SENSITIVE_KEY_COMPACTS)
    )


def sanitize_persisted_value(value: Any) -> Any:
    """Recursively redact credential-like values before repository persistence."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            result[key] = (
                _REDACTED if _is_sensitive_key(key) else sanitize_persisted_value(item)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_persisted_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _required(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
    return text


def _positive_seconds(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID) from exc
    if parsed <= 0:
        raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
    return parsed


@dataclass(frozen=True)
class A2ACompatibleInterface:
    protocol_binding: str
    protocol_version: str
    url: str

    def validate(self) -> "A2ACompatibleInterface":
        _required(self.protocol_binding, "protocol_binding")
        _required(self.protocol_version, "protocol_version")
        _required(self.url, "url")
        return self

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "A2ACompatibleInterface":
        return cls(
            protocol_binding=str(data.get("protocol_binding") or "").strip(),
            protocol_version=str(data.get("protocol_version") or "").strip(),
            url=str(data.get("url") or "").strip(),
        ).validate()


@dataclass(frozen=True)
class A2ADiscoveredAgent:
    name: str
    description: str = ""
    version: str = ""
    skills: tuple[dict[str, Any], ...] = ()
    compatible_interfaces: tuple[A2ACompatibleInterface, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "skills": sanitize_persisted_value(list(self.skills)),
            "compatible_interfaces": [
                asdict(item) for item in self.compatible_interfaces
            ],
        }


@dataclass(frozen=True)
class A2AOutboundDiscovery:
    """Short-lived discovery preview. It is deliberately not a callable registration."""

    discovery_id: str
    expires_at: str
    source_url: str
    card_path: str
    card_fingerprint: str
    agent: A2ADiscoveredAgent
    agent_card: dict[str, Any] = field(default_factory=dict)
    security_requirements: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovery_id": self.discovery_id,
            "expires_at": self.expires_at,
            "source_url": self.source_url,
            "card_path": self.card_path,
            "card_fingerprint": self.card_fingerprint,
            "agent": self.agent.to_dict(),
            "agent_card": sanitize_persisted_value(self.agent_card),
            "security_requirements": sanitize_persisted_value(
                list(self.security_requirements)
            ),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class A2AOutboundAgent:
    agent_id: str
    display_name: str
    source_url: str
    card_path: str
    card_fingerprint: str
    card_revision: int
    agent_card: dict[str, Any]
    selected_interface: A2ACompatibleInterface
    enabled: bool
    availability: A2AOutboundAvailability
    credential_ref: str | None
    connect_timeout_seconds: float
    sync_wait_seconds: float
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_error_code: str | None = None
    last_error_summary: str | None = None
    pending_revision: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""

    def validate(self) -> "A2AOutboundAgent":
        for name in (
            "agent_id",
            "display_name",
            "source_url",
            "card_path",
            "card_fingerprint",
            "created_at",
            "updated_at",
        ):
            _required(getattr(self, name), name)
        if int(self.card_revision) < 1:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        _positive_seconds(self.connect_timeout_seconds)
        _positive_seconds(self.sync_wait_seconds)
        self.selected_interface.validate()
        if self.credential_ref is not None:
            try:
                A2AOutboundCredentialStore.validate_for_agent(
                    self.agent_id,
                    _required(self.credential_ref, "credential_ref"),
                )
            except ValueError as exc:
                raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID) from exc
        return self

    def to_record(self) -> dict[str, Any]:
        self.validate()
        record = asdict(self)
        record["availability"] = self.availability.value
        record["agent_card"] = sanitize_persisted_value(self.agent_card)
        record["selected_interface"] = asdict(self.selected_interface)
        if self.pending_revision is not None:
            record["pending_revision"] = sanitize_persisted_value(self.pending_revision)
        return record

    def public_dict(self) -> dict[str, Any]:
        record = self.to_record()
        record["has_credential"] = bool(self.credential_ref)
        record.pop("credential_ref", None)
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "A2AOutboundAgent":
        try:
            item = cls(
                agent_id=str(record.get("agent_id") or "").strip(),
                display_name=str(record.get("display_name") or "").strip(),
                source_url=str(record.get("source_url") or "").strip(),
                card_path=str(record.get("card_path") or "").strip(),
                card_fingerprint=str(record.get("card_fingerprint") or "").strip(),
                card_revision=int(record.get("card_revision") or 0),
                agent_card=dict(record.get("agent_card") or {}),
                selected_interface=A2ACompatibleInterface.from_dict(
                    dict(record.get("selected_interface") or {})
                ),
                enabled=bool(record.get("enabled")),
                availability=A2AOutboundAvailability(
                    str(record.get("availability") or "")
                ),
                credential_ref=(
                    str(record.get("credential_ref")).strip()
                    if record.get("credential_ref") is not None
                    else None
                ),
                connect_timeout_seconds=float(
                    record.get("connect_timeout_seconds") or 0
                ),
                sync_wait_seconds=float(record.get("sync_wait_seconds") or 0),
                last_checked_at=record.get("last_checked_at"),
                last_success_at=record.get("last_success_at"),
                last_error_code=record.get("last_error_code"),
                last_error_summary=record.get("last_error_summary"),
                pending_revision=(
                    dict(record.get("pending_revision") or {})
                    if record.get("pending_revision") is not None
                    else None
                ),
                created_at=str(record.get("created_at") or ""),
                updated_at=str(record.get("updated_at") or ""),
            )
        except (TypeError, ValueError) as exc:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID) from exc
        return item.validate()


@dataclass(frozen=True)
class A2AOutboundDispatch:
    dispatch_id: str
    agent_id: str
    agent_revision: int
    mode: A2AOutboundDispatchMode
    status: A2AOutboundDispatchStatus
    request_message_id: str
    source_session_id: str
    created_at: str
    updated_at: str
    agent_name: str | None = None
    source_resource_id: str | None = None
    remote_task_id: str | None = None
    remote_context_id: str | None = None
    accepted_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_summary: str | None = None
    last_polled_at: str | None = None
    input_length: int | None = None
    input_content_type: str | None = None
    input_digest: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_DISPATCH_STATUSES

    def validate(self) -> "A2AOutboundDispatch":
        for name in (
            "dispatch_id",
            "agent_id",
            "request_message_id",
            "source_session_id",
            "created_at",
            "updated_at",
        ):
            _required(getattr(self, name), name)
        if int(self.agent_revision) < 1:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        if self.input_length is not None and int(self.input_length) < 0:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        return self

    def to_record(self) -> dict[str, Any]:
        self.validate()
        record = asdict(self)
        record["mode"] = self.mode.value
        record["status"] = self.status.value
        if self.result is not None:
            record["result"] = sanitize_persisted_value(self.result)
        return record

    def with_changes(self, **changes: Any) -> "A2AOutboundDispatch":
        return replace(self, **changes).validate()

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "A2AOutboundDispatch":
        try:
            item = cls(
                dispatch_id=str(record.get("dispatch_id") or "").strip(),
                agent_id=str(record.get("agent_id") or "").strip(),
                agent_revision=int(record.get("agent_revision") or 0),
                mode=A2AOutboundDispatchMode(str(record.get("mode") or "")),
                status=A2AOutboundDispatchStatus(str(record.get("status") or "")),
                request_message_id=str(record.get("request_message_id") or "").strip(),
                source_session_id=str(record.get("source_session_id") or "").strip(),
                created_at=str(record.get("created_at") or ""),
                updated_at=str(record.get("updated_at") or ""),
                agent_name=record.get("agent_name"),
                source_resource_id=record.get("source_resource_id"),
                remote_task_id=record.get("remote_task_id"),
                remote_context_id=record.get("remote_context_id"),
                accepted_at=record.get("accepted_at"),
                finished_at=record.get("finished_at"),
                result=(
                    dict(record.get("result") or {})
                    if record.get("result") is not None
                    else None
                ),
                error_code=record.get("error_code"),
                error_summary=record.get("error_summary"),
                last_polled_at=record.get("last_polled_at"),
                input_length=(
                    int(record.get("input_length"))
                    if record.get("input_length") is not None
                    else None
                ),
                input_content_type=record.get("input_content_type"),
                input_digest=record.get("input_digest"),
            )
        except (TypeError, ValueError) as exc:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID) from exc
        return item.validate()


__all__ = [
    "A2ACompatibleInterface",
    "A2ADiscoveredAgent",
    "A2AOutboundAgent",
    "A2AOutboundAvailability",
    "A2AOutboundDiscovery",
    "A2AOutboundDispatch",
    "A2AOutboundDispatchMode",
    "A2AOutboundDispatchStatus",
    "TERMINAL_DISPATCH_STATUSES",
    "sanitize_persisted_value",
]
