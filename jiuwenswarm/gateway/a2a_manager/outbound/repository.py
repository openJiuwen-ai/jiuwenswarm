"""PersistentStore repositories for A2A outbound agents and dispatches."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Mapping, Protocol

from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

from .credentials import A2AOutboundCredentialStore
from .errors import safe_error_summary
from .locks import KeyedLockPool
from .models import (
    A2AOutboundAgent,
    A2AOutboundAvailability,
    A2AOutboundDispatch,
    A2AOutboundDispatchStatus,
    TERMINAL_DISPATCH_STATUSES,
)

A2A_OUTBOUND_AGENT_STORE_NAME = "a2a_outbound_agent"
A2A_OUTBOUND_DISPATCH_STORE_NAME = "a2a_outbound_dispatch"
DEFAULT_DISPATCH_RETENTION_MAX_RECORDS = 1_000
DEFAULT_DISPATCH_RETENTION_DAYS = 30


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class A2AOutboundRecordCodec(Protocol):
    def agent_identity(self, agent_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def dispatch_identity(self, dispatch_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def list_filters(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def agent_to_record(self, agent: A2AOutboundAgent) -> dict[str, Any]:
        raise NotImplementedError

    def agent_from_record(self, record: Mapping[str, Any]) -> A2AOutboundAgent:
        raise NotImplementedError

    def dispatch_to_record(self, dispatch: A2AOutboundDispatch) -> dict[str, Any]:
        raise NotImplementedError

    def dispatch_from_record(
        self, record: Mapping[str, Any]
    ) -> A2AOutboundDispatch:
        raise NotImplementedError


class JsonA2AOutboundRecordCodec:
    """Personal-edition JSON shape containing the complete domain record."""

    @staticmethod
    def agent_identity(agent_id: str) -> dict[str, Any]:
        return {"agent_id": str(agent_id)}

    @staticmethod
    def dispatch_identity(dispatch_id: str) -> dict[str, Any]:
        return {"dispatch_id": str(dispatch_id)}

    @staticmethod
    def list_filters() -> dict[str, Any] | None:
        return None

    @staticmethod
    def agent_to_record(agent: A2AOutboundAgent) -> dict[str, Any]:
        return agent.to_record()

    @staticmethod
    def agent_from_record(record: Mapping[str, Any]) -> A2AOutboundAgent:
        return A2AOutboundAgent.from_record(record)

    @staticmethod
    def dispatch_to_record(dispatch: A2AOutboundDispatch) -> dict[str, Any]:
        return dispatch.to_record()

    @staticmethod
    def dispatch_from_record(record: Mapping[str, Any]) -> A2AOutboundDispatch:
        return A2AOutboundDispatch.from_record(record)


class A2AOutboundRepository:
    """Personal-edition domain repository with per-entity transition locks."""

    def __init__(
        self,
        store: PersistentStore,
        codec: A2AOutboundRecordCodec | None = None,
        credential_store: A2AOutboundCredentialStore | None = None,
    ) -> None:
        self._store = store
        self._codec = codec or JsonA2AOutboundRecordCodec()
        self._credential_store = credential_store or A2AOutboundCredentialStore()
        self._agent_locks = KeyedLockPool()
        self._dispatch_locks = KeyedLockPool()
        self._discovery_locks = KeyedLockPool()
        self._retention_lock = asyncio.Lock()

    @asynccontextmanager
    async def hold_discovery(self, discovery_id: str) -> AsyncIterator[None]:
        """Serialize registration attempts that consume the same discovery result."""
        async with self._discovery_locks.hold(discovery_id):
            yield

    async def get_agent(self, agent_id: str) -> A2AOutboundAgent | None:
        row = await self._store.get(
            A2A_OUTBOUND_AGENT_STORE_NAME,
            self._codec.agent_identity(agent_id),
        )
        return None if row is None else self._codec.agent_from_record(row)

    async def list_agents(self) -> list[A2AOutboundAgent]:
        rows = await self._store.list(
            A2A_OUTBOUND_AGENT_STORE_NAME,
            filters=self._codec.list_filters(),
            order_by="updated_at DESC",
            limit=None,
        )
        return [self._codec.agent_from_record(row) for row in rows]

    async def create_agent(self, agent: A2AOutboundAgent) -> A2AOutboundAgent:
        agent.validate()
        async with self._agent_locks.hold(agent.agent_id):
            created = await self._store.create(
                A2A_OUTBOUND_AGENT_STORE_NAME,
                self._codec.agent_to_record(agent),
            )
            return self._codec.agent_from_record(created)

    async def update_agent(
        self,
        agent_id: str,
        updater: Callable[[A2AOutboundAgent], A2AOutboundAgent],
    ) -> A2AOutboundAgent | None:
        """Read, mutate, and persist an Agent while holding its per-Agent lock."""
        async with self._agent_locks.hold(agent_id):
            key = self._codec.agent_identity(agent_id)
            row = await self._store.get(A2A_OUTBOUND_AGENT_STORE_NAME, key)
            if row is None:
                return None
            current = self._codec.agent_from_record(row)
            updated_agent = updater(current).validate()
            if updated_agent.agent_id != current.agent_id:
                raise ValueError("agent updater cannot change agent_id")
            record = self._codec.agent_to_record(updated_agent)
            updates = {name: value for name, value in record.items() if name not in key}
            updated = await self._store.update(
                A2A_OUTBOUND_AGENT_STORE_NAME,
                key,
                updates,
            )
            return None if updated is None else self._codec.agent_from_record(updated)

    async def delete_agent(self, agent_id: str) -> bool:
        async with self._agent_locks.hold(agent_id):
            key = self._codec.agent_identity(agent_id)
            row = await self._store.get(A2A_OUTBOUND_AGENT_STORE_NAME, key)
            if row is None:
                return False
            agent = self._codec.agent_from_record(row)
            if agent.credential_ref:
                credential_ref = self._credential_store.validate_for_agent(
                    agent.agent_id,
                    agent.credential_ref,
                )
                # Delete the secret first. If record deletion then fails, a retry is
                # safe and the remaining registration cannot authenticate remotely.
                self._credential_store.delete(credential_ref)
            return await self._store.delete(
                A2A_OUTBOUND_AGENT_STORE_NAME,
                key,
            )

    async def update_runtime_state(
        self,
        agent_id: str,
        availability: A2AOutboundAvailability | str,
        *,
        error_code: str | None = None,
    ) -> None:
        """Persist runtime health when the repository provides a separate store."""
        del agent_id, availability, error_code

    async def get_dispatch(self, dispatch_id: str) -> A2AOutboundDispatch | None:
        row = await self._store.get(
            A2A_OUTBOUND_DISPATCH_STORE_NAME,
            self._codec.dispatch_identity(dispatch_id),
        )
        return None if row is None else self._codec.dispatch_from_record(row)

    async def list_dispatches(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[A2AOutboundDispatch]:
        rows = await self._store.list(
            A2A_OUTBOUND_DISPATCH_STORE_NAME,
            filters=self._codec.list_filters(),
            order_by="created_at DESC",
            limit=None if limit is None else max(0, int(limit)),
            offset=max(0, int(offset)),
        )
        return [self._codec.dispatch_from_record(row) for row in rows]

    async def count_dispatches(self) -> int:
        """Return the total matching record count, skipping row decoding."""
        rows = await self._store.list(
            A2A_OUTBOUND_DISPATCH_STORE_NAME,
            filters=self._codec.list_filters(),
        )
        return len(rows)

    async def create_dispatch(
        self,
        dispatch: A2AOutboundDispatch,
    ) -> A2AOutboundDispatch:
        dispatch.validate()
        async with self._dispatch_locks.hold(dispatch.dispatch_id):
            created = await self._store.create(
                A2A_OUTBOUND_DISPATCH_STORE_NAME,
                self._codec.dispatch_to_record(dispatch),
            )
            return self._codec.dispatch_from_record(created)

    async def transition_dispatch(
        self,
        dispatch_id: str,
        status: A2AOutboundDispatchStatus | str,
        *,
        updated_at: str | None = None,
        **changes: Any,
    ) -> A2AOutboundDispatch | None:
        """Apply a locked state transition; an existing terminal state is immutable."""
        normalized_status = A2AOutboundDispatchStatus(status)
        allowed_changes = {
            "remote_task_id",
            "remote_context_id",
            "accepted_at",
            "finished_at",
            "result",
            "error_code",
            "error_summary",
            "last_polled_at",
        }
        unknown = set(changes) - allowed_changes
        if unknown:
            raise ValueError(
                f"unsupported dispatch fields: {', '.join(sorted(unknown))}"
            )

        async with self._dispatch_locks.hold(dispatch_id):
            row = await self._store.get(
                A2A_OUTBOUND_DISPATCH_STORE_NAME,
                self._codec.dispatch_identity(dispatch_id),
            )
            if row is None:
                return None
            current = self._codec.dispatch_from_record(row)
            if current.status in TERMINAL_DISPATCH_STATUSES:
                return current

            # Lifecycle timestamps record the first observation and must not
            # drift on later working/poll events.
            if current.accepted_at is not None:
                changes["accepted_at"] = current.accepted_at

            stamp = updated_at or utc_now_text()
            if normalized_status in TERMINAL_DISPATCH_STATUSES:
                changes.setdefault("finished_at", stamp)
            error_code = changes.get("error_code")
            if error_code:
                changes["error_summary"] = safe_error_summary(error_code)
            next_value = replace(
                current,
                status=normalized_status,
                updated_at=stamp,
                **changes,
            ).validate()
            record = self._codec.dispatch_to_record(next_value)
            key = self._codec.dispatch_identity(dispatch_id)
            updates = {name: value for name, value in record.items() if name not in key}
            # Include the observed state in the update predicate so the personal
            # file backend cannot overwrite a terminal state with a late event.
            compare_key = {**key, "status": current.status.value}
            updated = await self._store.update(
                A2A_OUTBOUND_DISPATCH_STORE_NAME,
                compare_key,
                updates,
            )
            if updated is not None:
                return self._codec.dispatch_from_record(updated)
            # Another writer may have won the comparison. Return the persisted
            # winner, never the uncommitted candidate.
            latest = await self._store.get(A2A_OUTBOUND_DISPATCH_STORE_NAME, key)
            return None if latest is None else self._codec.dispatch_from_record(latest)

    async def cleanup_dispatches(
        self,
        *,
        now: datetime | None = None,
        max_records: int = DEFAULT_DISPATCH_RETENTION_MAX_RECORDS,
        max_age_days: int = DEFAULT_DISPATCH_RETENTION_DAYS,
    ) -> int:
        """Delete terminal records older than either enabled retention boundary."""
        max_records = max(0, int(max_records))
        max_age_days = max(0, int(max_age_days))
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        cutoff = (
            current_time.astimezone(timezone.utc) - timedelta(days=max_age_days)
            if max_age_days > 0
            else None
        )

        async with self._retention_lock:
            records = await self.list_dispatches()
            ordered = sorted(
                (item for item in records if item.is_terminal),
                key=lambda item: (
                    _parse_timestamp(item.created_at)
                    or datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
            expired_ids: set[str] = set()
            if cutoff is not None:
                for item in ordered:
                    created_at = _parse_timestamp(item.created_at)
                    if (
                        created_at or datetime.min.replace(tzinfo=timezone.utc)
                    ) < cutoff:
                        expired_ids.add(item.dispatch_id)
            survivors = [
                item for item in ordered if item.dispatch_id not in expired_ids
            ]
            overflow = max(0, len(survivors) - max_records) if max_records > 0 else 0
            expired_ids.update(item.dispatch_id for item in survivors[:overflow])

            deleted = 0
            for dispatch_id in expired_ids:
                async with self._dispatch_locks.hold(dispatch_id):
                    if await self._store.delete(
                        A2A_OUTBOUND_DISPATCH_STORE_NAME,
                        self._codec.dispatch_identity(dispatch_id),
                    ):
                        deleted += 1
            return deleted


__all__ = [
    "A2A_OUTBOUND_AGENT_STORE_NAME",
    "A2A_OUTBOUND_DISPATCH_STORE_NAME",
    "A2AOutboundRecordCodec",
    "A2AOutboundRepository",
    "DEFAULT_DISPATCH_RETENTION_DAYS",
    "DEFAULT_DISPATCH_RETENTION_MAX_RECORDS",
    "JsonA2AOutboundRecordCodec",
    "utc_now_text",
]
