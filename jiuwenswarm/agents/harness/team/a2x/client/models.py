"""Response dataclasses for the A2X Registry client SDK.

Each class wraps a specific backend response shape. ``from_dict`` factories
tolerate unknown fields for forward compatibility; ``AgentDetail.raw`` keeps
the complete untouched response so callers can read fields the SDK has not
yet declared (e.g. ``status``, ``endpoint``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T", bound="_FromDictMixin")


class _FromDictMixin:
    """Shared ``from_dict`` that drops unknown keys."""

    @classmethod
    def from_dict(cls: type[_T], data: dict[str, Any]) -> _T:
        allowed = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        kwargs = {k: v for k, v in data.items() if k in allowed}
        return cls(**kwargs)  # type: ignore[call-arg]


@dataclass
class DatasetCreateResponse(_FromDictMixin):
    dataset: str
    embedding_model: str
    formats: dict[str, Any]
    status: str


@dataclass
class DatasetDeleteResponse(_FromDictMixin):
    dataset: str
    status: str


@dataclass
class RegisterResponse(_FromDictMixin):
    service_id: str
    dataset: str
    status: str


@dataclass
class PatchResponse(_FromDictMixin):
    service_id: str
    dataset: str
    status: str
    changed_fields: list[str] = field(default_factory=list)
    taxonomy_affected: bool = False


@dataclass
class DeregisterResponse(_FromDictMixin):
    """Successful deregister. ``status`` is always ``"deregistered"`` —
    a missing service surfaces as ``NotFoundError`` (HTTP 404), not as a
    200 with ``status="not_found"``."""

    service_id: str
    status: str


@dataclass
class AgentDetail:
    """Full single-agent response. ``metadata`` is the complete Agent Card."""

    id: str
    type: str
    name: str
    description: str
    metadata: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentDetail":
        return cls(
            id=data.get("id", ""),
            type=data.get("type", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            metadata=data.get("metadata") or {},
            raw=dict(data),
        )


@dataclass
class Reservation:
    """A successful reservation — leader's handle to a leased agent set.

    Acts as a context manager. On ``__exit__`` / ``__aexit__`` it
    best-effort releases all leases under ``holder_id`` (idempotent — a
    later explicit release is a no-op).

    The async variant requires the matching ``AsyncA2XRegistryClient``; storing the
    client reference lets the context-manager release path work even after
    the calling scope's local variable is gone.
    """

    holder_id: str
    dataset: str
    ttl_seconds: int
    expires_at_unix: float
    agents: list[dict[str, Any]]
    # Reference to the parent client so __exit__ can call release.
    # Untyped (Any) to avoid a circular import with client.py.
    _client: Any = None
    _released: bool = False

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        dataset: str,
        client: Any,
    ) -> "Reservation":
        return cls(
            holder_id=data["holder_id"],
            dataset=dataset,
            ttl_seconds=int(data.get("ttl_seconds", 30)),
            expires_at_unix=float(data.get("expires_at_unix", 0.0)),
            agents=list(data.get("reservations") or []),
            _client=client,
        )

    # ── sync context manager ─────────────────────────────────────────────
    def __enter__(self) -> "Reservation":
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._released or self._client is None:
            return
        try:
            self._client.release_reservation(self)
        except Exception:
            logger.warning(
                "Reservation release on __exit__ failed for holder_id=%s; "
                "lease will TTL-expire",
                self.holder_id,
                exc_info=True,
            )
        finally:
            self._released = True

    # ── async context manager ────────────────────────────────────────────
    async def __aenter__(self) -> "Reservation":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._released or self._client is None:
            return
        try:
            await self._client.release_reservation(self)
        except Exception:
            logger.warning(
                "Reservation release on __aexit__ failed for holder_id=%s; "
                "lease will TTL-expire",
                self.holder_id,
                exc_info=True,
            )
        finally:
            self._released = True
