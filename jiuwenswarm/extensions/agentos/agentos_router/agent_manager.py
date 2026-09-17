# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.extensions.agentos.agentos_router.models import AgentInfo, AgentStatus


AgentCreator = Callable[[AgentInfo], Awaitable[AgentInfo | None]]
AgentKey = tuple[str, ...]
BUILTIN_AGENT_TYPE = "jiuwenswarm"
SUPPORTED_AGENT_KEY_FIELDS = frozenset({"user_id", "agent_type", "session_id"})
DEFAULT_AGENT_KEY_FIELDS = ("user_id", "agent_type")


def is_third_party_agent_type(agent_type: str) -> bool:
    """True when *agent_type* is not the builtin swarm type."""
    return str(agent_type or "").strip().lower() not in {"", BUILTIN_AGENT_TYPE}


def normalize_agent_key_fields(raw: Any = None) -> tuple[str, ...]:
    """Normalize configured agent key fields.

    Default is ``user_id + agent_type``. Supported fields:
    ``user_id``, ``agent_type``, ``session_id``. Both ``user_id`` and
    ``agent_type`` are always required.
    """

    if raw is None:
        fields = list(DEFAULT_AGENT_KEY_FIELDS)
    elif isinstance(raw, str):
        text = raw.strip().lower().replace("+", ",").replace("|", ",")
        fields = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        fields = [str(part).strip().lower() for part in raw if str(part).strip()]
    else:
        raise ValueError(
            "agent_key_fields must be a string or list, "
            f"got {type(raw).__name__}"
        )

    if not fields:
        fields = list(DEFAULT_AGENT_KEY_FIELDS)

    unique: list[str] = []
    seen: set[str] = set()
    for name in fields:
        if name not in SUPPORTED_AGENT_KEY_FIELDS:
            raise ValueError(f"unsupported agent_key_field: {name}")
        if name not in seen:
            seen.add(name)
            unique.append(name)

    if "user_id" not in seen or "agent_type" not in seen:
        raise ValueError("agent_key_fields must include user_id and agent_type")
    return tuple(unique)


class AgentCreatingTimeout(TimeoutError):
    """Timed out while waiting for another request to create an Agent."""


class AgentDeleted(RuntimeError):
    """Agent was deleted while creation was in flight."""


class AgentCreateFailed(RuntimeError):
    """Previous create failed; automatic retry is disabled to avoid double create."""


@dataclass
class AgentRuntime:
    """In-process agent record: business info plus create-wait signaling."""

    info: AgentInfo
    key: AgentKey
    creating_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Sandbox activity accounting: task_count is held for the whole lifetime
    # of a chat request / stream / SSH relay; the idle clock only starts once
    # it drops back to zero.
    task_count: int = 0
    last_active_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_active_at = time.time()

    def is_ready(self) -> bool:
        return self.info.status is AgentStatus.READY

    def is_creating(self) -> bool:
        return self.info.status is AgentStatus.CREATING

    def is_failed(self) -> bool:
        return self.info.status is AgentStatus.FAILED

    def is_deleted(self) -> bool:
        return self.info.status is AgentStatus.DELETED

    def snapshot(self) -> AgentRuntime:
        """Return a detached runtime view for callers (info only)."""
        return AgentRuntime(
            info=self.info.copy(),
            key=self.key,
            task_count=self.task_count,
            last_active_at=self.last_active_at,
        )

    def attach_to_envelope(self, envelope: E2AEnvelope) -> None:
        envelope.channel_context["agent_id"] = self.info.agent_id
        envelope.channel_context["agent_type"] = self.info.agent_type
        if self.info.sandbox_id:
            envelope.channel_context["sandbox_id"] = self.info.sandbox_id

    @staticmethod
    def apply_creator_result(created: AgentInfo | None, *, base: AgentInfo) -> AgentInfo:
        resolved = created.copy() if created is not None else base.copy()
        resolved.agent_id = base.agent_id
        resolved.user_id = base.user_id
        resolved.agent_type = base.agent_type
        resolved.status = AgentStatus.READY
        resolved.error = None
        resolved.updated_at = time.time()
        return resolved

    def mark_ready(self, resolved: AgentInfo) -> None:
        self.info = resolved
        self.creating_event.set()

    def mark_failed(self, exc: BaseException) -> None:
        failed = self.info.copy()
        failed.status = AgentStatus.FAILED
        failed.error = str(exc)
        failed.updated_at = time.time()
        self.info = failed
        self.creating_event.set()

    def mark_deleted(self) -> None:
        deleted = self.info.copy()
        deleted.status = AgentStatus.DELETED
        deleted.error = "agent deleted"
        deleted.updated_at = time.time()
        self.info = deleted
        self.creating_event.set()

    async def wait_until_settled(self, timeout: float) -> None:
        await asyncio.wait_for(self.creating_event.wait(), timeout=timeout)

    @staticmethod
    def normalize_agent_type(raw: Any) -> str:
        """Normalize agent_type; any non-empty name is accepted (no allowlist)."""
        agent_type = str(raw or "").strip().lower()
        return agent_type or BUILTIN_AGENT_TYPE

    @staticmethod
    def normalize_session_id(raw: Any) -> str:
        session_id = str(raw or "").strip()
        if not session_id:
            raise ValueError("session_id is required by agent_key_fields")
        return session_id

    @staticmethod
    def normalize_key_value(field_name: str, raw: Any) -> str:
        if field_name == "user_id":
            value = str(raw or "").strip()
            if not value:
                raise ValueError("user_id is required")
            return value
        if field_name == "agent_type":
            return AgentRuntime.normalize_agent_type(raw)
        if field_name == "session_id":
            return AgentRuntime.normalize_session_id(raw)
        value = str(raw or "").strip()
        if not value:
            raise ValueError(f"{field_name} is required by agent_key_fields")
        return value

    @staticmethod
    def format_key(key: AgentKey, key_fields: Sequence[str]) -> str:
        parts = [
            f"{field_name}={value}"
            for field_name, value in zip(key_fields, key, strict=False)
        ]
        return " ".join(parts)

    @classmethod
    def build_key(
        cls,
        key_fields: Sequence[str],
        *,
        user_id: str,
        agent_type: str,
        key_values: Mapping[str, Any] | None = None,
    ) -> AgentKey:
        values: dict[str, Any] = {
            "user_id": user_id,
            "agent_type": agent_type,
            **dict(key_values or {}),
        }
        return tuple(
            cls.normalize_key_value(name, values.get(name)) for name in key_fields
        )

    @classmethod
    def for_key(
        cls,
        key: AgentKey,
        *,
        user_id: str,
        agent_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRuntime:
        return cls(
            key=key,
            info=AgentInfo(
                user_id=user_id,
                agent_type=agent_type,
                metadata=dict(metadata or {}),
            ),
        )


class AgentManager:
    """In-memory Agent store with configurable key fields and single-flight creation."""

    def __init__(
        self,
        *,
        creating_timeout_seconds: float = 60.0,
        key_fields: Sequence[str] | str | None = None,
    ) -> None:
        self._key_fields = normalize_agent_key_fields(key_fields)
        self._runtimes: dict[AgentKey, AgentRuntime] = {}
        self._runtimes_lock = asyncio.Lock()
        self._creating_timeout_seconds = max(0.1, float(creating_timeout_seconds))
        # 用户连接计数：user_id → 当前活跃连接数
        self._user_connection_counts: dict[str, int] = {}

    @property
    def key_fields(self) -> tuple[str, ...]:
        return self._key_fields

    @staticmethod
    def _failed_message(runtime: AgentRuntime, key_desc: str) -> str:
        detail = str(runtime.info.error or "").strip() or "unknown error"
        return f"AGENT_CREATE_FAILED: {key_desc}: {detail}"

    # ── 用户连接计数 ──

    def increment_user_connections(self, user_id: str) -> None:
        """递增用户连接计数。"""
        uid = str(user_id or "").strip()
        self._user_connection_counts[uid] = self._user_connection_counts.get(uid, 0) + 1

    def decrement_user_connections(self, user_id: str) -> int:
        """递减用户连接计数，返回递减后的值（最小为 0）。"""
        uid = str(user_id or "").strip()
        count = self._user_connection_counts.get(uid, 0) - 1
        if count <= 0:
            self._user_connection_counts.pop(uid, None)
            return 0
        self._user_connection_counts[uid] = count
        return count

    def get_user_connection_count(self, user_id: str) -> int:
        """获取用户当前连接数。"""
        uid = str(user_id or "").strip()
        return self._user_connection_counts.get(uid, 0)

    def _make_key(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: Mapping[str, Any] | None = None,
    ) -> AgentKey:
        return AgentRuntime.build_key(
            self._key_fields,
            user_id=user_id,
            agent_type=agent_type,
            key_values=key_values,
        )

    def _identity_from_key(self, key: AgentKey) -> tuple[str, str]:
        values = dict(zip(self._key_fields, key, strict=False))
        return values["user_id"], values["agent_type"]

    @staticmethod
    def _acquire_locked(runtime: AgentRuntime) -> None:
        """Increment task holding under ``_runtimes_lock``."""
        runtime.task_count += 1
        runtime.touch()

    async def get_or_create_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: Mapping[str, Any] | None = None,
        creator: AgentCreator | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        acquire: bool = False,
    ) -> AgentRuntime:
        """Get a READY Agent runtime or create one, waiting for in-flight creation.

        With ``acquire=True`` the stored runtime's ``task_count`` is
        incremented atomically before the snapshot is returned, so the idle
        reaper can never reclaim an agent between resolve and use. Callers
        must pair it with :meth:`release`.
        """

        key = self._make_key(user_id, agent_type, key_values=key_values)
        key_user_id, key_agent_type = self._identity_from_key(key)
        wait_timeout = (
            self._creating_timeout_seconds
            if timeout_seconds is None
            else max(0.1, float(timeout_seconds))
        )
        key_desc = AgentRuntime.format_key(key, self._key_fields)

        while True:
            owner = False
            async with self._runtimes_lock:
                existing = self._runtimes.get(key)
                if existing is not None and existing.is_ready():
                    if acquire:
                        self._acquire_locked(existing)
                    else:
                        # Plain resolve (e.g. 3rdagent.switch) still counts as
                        # activity for the idle clock.
                        existing.touch()
                    return existing.snapshot()

                if existing is None:
                    runtime = AgentRuntime.for_key(
                        key,
                        user_id=key_user_id,
                        agent_type=key_agent_type,
                        metadata=metadata,
                    )
                    self._runtimes[key] = runtime
                    owner = True
                else:
                    runtime = existing
                    if runtime.is_failed():
                        # Do not reset_for_retry: a cancelled/failed create may
                        # still have provisioned a YuanRong sandbox; retrying
                        # here would spawn a second one.
                        raise AgentCreateFailed(
                            self._failed_message(runtime, key_desc)
                        )
                creator_base = runtime.info.copy()

            if owner:
                return await self._run_creator(
                    key,
                    creator_base,
                    creator,
                    owner_runtime=runtime,
                    acquire=acquire,
                )

            try:
                await runtime.wait_until_settled(wait_timeout)
            except asyncio.TimeoutError as exc:
                raise AgentCreatingTimeout(
                    f"AGENT_CREATING_TIMEOUT: {key_desc}"
                ) from exc
            if runtime.is_deleted():
                raise AgentDeleted(f"AGENT_DELETED: {key_desc}")
            if runtime.is_failed():
                raise AgentCreateFailed(self._failed_message(runtime, key_desc))

    async def get_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: Mapping[str, Any] | None = None,
        acquire: bool = False,
    ) -> AgentRuntime | None:
        """Return a snapshot of an existing runtime, or ``None``.

        With ``acquire=True`` and a READY runtime, increment ``task_count``
        under the lock (same contract as :meth:`get_or_create_agent`) so the
        idle reaper cannot reclaim the sandbox between resolve and use.
        Callers must pair it with :meth:`release`.
        """
        key = self._make_key(user_id, agent_type, key_values=key_values)
        async with self._runtimes_lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                return None
            if acquire and runtime.is_ready():
                self._acquire_locked(runtime)
            return runtime.snapshot()

    async def delete_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: Mapping[str, Any] | None = None,
    ) -> None:
        key = self._make_key(user_id, agent_type, key_values=key_values)
        async with self._runtimes_lock:
            runtime = self._runtimes.pop(key, None)
        if runtime is not None:
            runtime.mark_deleted()

    async def release(self, key: AgentKey) -> None:
        """Drop one task holding acquired via ``get_or_create_agent`` / ``get_agent``.

        No-op when the runtime is already gone (e.g. explicitly deleted).
        """
        async with self._runtimes_lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                return
            runtime.task_count = max(0, runtime.task_count - 1)
            runtime.touch()

    async def pop_if_idle(
        self,
        key: AgentKey,
        idle_timeout_seconds: float,
    ) -> AgentRuntime | None:
        """Atomically remove a READY runtime idle beyond the timeout.

        Returns a detached snapshot (status DELETED) when reclaimed so the
        caller can release the underlying sandbox, or ``None`` when the
        runtime is missing, still held (``task_count > 0``), not READY, or
        not idle long enough.
        """
        timeout = float(idle_timeout_seconds)
        if timeout <= 0:
            return None
        async with self._runtimes_lock:
            runtime = self._runtimes.get(key)
            if runtime is None or not runtime.is_ready():
                return None
            if runtime.task_count > 0:
                return None
            if time.time() - runtime.last_active_at < timeout:
                return None
            self._runtimes.pop(key, None)
        runtime.mark_deleted()
        return runtime.snapshot()

    async def list_keys(self) -> list[AgentKey]:
        async with self._runtimes_lock:
            return list(self._runtimes.keys())

    async def list_user_agents(self, user_id: str) -> list[AgentRuntime]:
        normalized_user_id = str(user_id or "").strip()
        async with self._runtimes_lock:
            return [
                runtime.snapshot()
                for runtime in self._runtimes.values()
                if runtime.info.user_id == normalized_user_id
            ]

    async def _mark_creator_failed(
        self,
        key: AgentKey,
        exc: BaseException,
        *,
        owner_runtime: AgentRuntime,
    ) -> None:
        async with self._runtimes_lock:
            if self._runtimes.get(key) is not owner_runtime:
                return
            owner_runtime.mark_failed(exc)

    async def _run_creator(
        self,
        key: AgentKey,
        agent: AgentInfo,
        creator: AgentCreator | None,
        *,
        owner_runtime: AgentRuntime,
        acquire: bool = False,
    ) -> AgentRuntime:
        key_desc = AgentRuntime.format_key(key, self._key_fields)
        try:
            created = await creator(agent.copy()) if creator is not None else agent
            resolved = AgentRuntime.apply_creator_result(created, base=agent)
            async with self._runtimes_lock:
                if self._runtimes.get(key) is not owner_runtime:
                    raise AgentDeleted(f"AGENT_DELETED: {key_desc}")
                owner_runtime.mark_ready(resolved)
                if acquire:
                    self._acquire_locked(owner_runtime)
                return owner_runtime.snapshot()
        except asyncio.CancelledError as exc:
            await self._mark_creator_failed(
                key, exc, owner_runtime=owner_runtime
            )
            raise
        except AgentDeleted:
            raise
        except Exception as exc:
            await self._mark_creator_failed(
                key, exc, owner_runtime=owner_runtime
            )
            raise
