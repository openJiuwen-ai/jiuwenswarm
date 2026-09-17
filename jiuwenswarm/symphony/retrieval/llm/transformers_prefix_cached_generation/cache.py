from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ..base import PrefixCacheUnavailable

logger = logging.getLogger(__name__)

SlotState = Literal["idle", "busy", "poisoned", "rebuilding", "disabled"]
ReplicaState = Literal["healthy", "degraded", "draining", "disabled"]


@dataclass(frozen=True)
class PrefixCacheEntry:
    cache_id: str
    prefix_input_ids: Any | None
    prefix_len: int
    past_key_values: Any
    attention_mask_prefix: Any | None = None
    next_cache_position: int = 0
    device: str = ""
    dtype: str = ""
    readonly: bool = True


@dataclass
class PrefixStaticCacheSlot:
    slot_id: str
    cache_id: str
    static_cache: Any
    replica_id: int
    tp_rank_devices: tuple[int, ...]
    prefix_len: int
    max_cache_len: int
    prefix_messages: tuple[dict[str, str], ...] = ()
    state: SlotState = "idle"
    active_len: int = 0
    poison_reason: str = ""


@dataclass
class PrefixCacheReplica:
    replica_id: int
    tp_rank_devices: tuple[int, ...]
    state: ReplicaState = "healthy"
    degraded_reason: str = ""

    def mark_degraded(self, *, reason: str) -> None:
        if self.state == "disabled":
            return
        self.state = "degraded"
        self.degraded_reason = str(reason or "").strip()
        logger.debug(
            "prefix cache replica degraded replica_id=%s reason=%s",
            self.replica_id,
            self.degraded_reason,
        )

    def mark_healthy(self) -> None:
        if self.state != "disabled":
            self.state = "healthy"
            self.degraded_reason = ""
            logger.debug("prefix cache replica healthy replica_id=%s", self.replica_id)


class PrefixStaticCachePool:
    def __init__(
        self,
        *,
        cache_id: str,
        replica: PrefixCacheReplica,
        slots: Sequence[PrefixStaticCacheSlot],
    ) -> None:
        self.cache_id = str(cache_id)
        self.replica = replica
        self._slots = list(slots)
        self._condition = threading.Condition()

    @property
    def slots(self) -> tuple[PrefixStaticCacheSlot, ...]:
        return tuple(self._slots)

    def acquire(self) -> PrefixStaticCacheSlot:
        with self._condition:
            logger.debug(
                "prefix cache slot acquire start cache_id=%s replica_id=%s idle=%s",
                self.cache_id,
                self.replica.replica_id,
                sum(1 for slot in self._slots if slot.state == "idle"),
            )
            if self.replica.state != "disabled":
                for slot in self._slots:
                    if slot.state == "idle":
                        slot.state = "busy"
                        slot.active_len = int(slot.prefix_len)
                        logger.debug(
                            "prefix cache slot acquired cache_id=%s slot_id=%s replica_id=%s "
                            "prefix_len=%s max_cache_len=%s",
                            self.cache_id,
                            slot.slot_id,
                            self.replica.replica_id,
                            slot.prefix_len,
                            slot.max_cache_len,
                        )
                        return slot
            logger.debug(
                "prefix cache slot acquire rejected cache_id=%s replica_id=%s replica_state=%s",
                self.cache_id,
                self.replica.replica_id,
                self.replica.state,
            )
            raise PrefixCacheUnavailable(f"prefix cache pool exhausted: {self.cache_id}")

    def release(self, slot: PrefixStaticCacheSlot) -> None:
        with self._condition:
            if slot.state == "busy":
                slot.active_len = int(slot.prefix_len)
                slot.state = "idle"
                logger.debug(
                    "prefix cache slot released cache_id=%s slot_id=%s active_len=%s",
                    self.cache_id,
                    slot.slot_id,
                    slot.active_len,
                )
            self._condition.notify()

    def mark_poisoned(self, slot: PrefixStaticCacheSlot, *, reason: str) -> None:
        with self._condition:
            slot.state = "poisoned"
            slot.poison_reason = str(reason or "").strip()
            logger.debug(
                "prefix cache slot poisoned cache_id=%s slot_id=%s reason=%s",
                self.cache_id,
                slot.slot_id,
                slot.poison_reason,
            )
            self._condition.notify()

    def disable_slot(self, slot: PrefixStaticCacheSlot, *, reason: str) -> None:
        with self._condition:
            slot.state = "disabled"
            slot.poison_reason = str(reason or "").strip()
            logger.debug(
                "prefix cache slot disabled cache_id=%s slot_id=%s reason=%s",
                self.cache_id,
                slot.slot_id,
                slot.poison_reason,
            )
            self._condition.notify()

    def mark_rebuilt(self, slot: PrefixStaticCacheSlot) -> None:
        with self._condition:
            slot.poison_reason = ""
            slot.active_len = int(slot.prefix_len)
            slot.state = "idle"
            logger.debug(
                "prefix cache slot rebuilt cache_id=%s slot_id=%s prefix_len=%s max_cache_len=%s",
                self.cache_id,
                slot.slot_id,
                slot.prefix_len,
                slot.max_cache_len,
            )
            self._condition.notify()

    def idle_count(self) -> int:
        with self._condition:
            return sum(1 for slot in self._slots if slot.state == "idle")


@dataclass(frozen=True)
class RequestCacheState:
    handle: "PrefixCacheHandle"
    pool: PrefixStaticCachePool
    slot: PrefixStaticCacheSlot
    suffix_token_ids: tuple[int, ...]
    max_new_tokens: int


@dataclass(frozen=True)
class PrefixCacheHandle:
    cache_id: str
    pools: tuple[PrefixStaticCachePool, ...]
    prefix_len: int
    prefix_token_hash: str
    model_fingerprint: str
    tokenizer_fingerprint: str
    chat_template_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dp_replica_id(self) -> int | None:
        return self.pools[0].replica.replica_id if self.pools else None

    def acquire_slot(self) -> tuple[PrefixStaticCachePool, PrefixStaticCacheSlot]:
        ordered = sorted(
            self.pools,
            key=lambda pool: (
                0 if pool.replica.state == "healthy" else 1,
                -pool.idle_count(),
                pool.replica.replica_id,
            ),
        )
        last_error: Exception | None = None
        for pool in ordered:
            if pool.replica.state == "disabled":
                logger.debug(
                    "prefix cache skipping disabled replica cache_id=%s replica_id=%s",
                    self.cache_id,
                    pool.replica.replica_id,
                )
                continue
            try:
                return pool, pool.acquire()
            except PrefixCacheUnavailable as exc:
                last_error = exc
                logger.debug(
                    "prefix cache replica unavailable cache_id=%s replica_id=%s reason=%s",
                    self.cache_id,
                    pool.replica.replica_id,
                    exc,
                )
                continue
        if last_error is not None:
            raise PrefixCacheUnavailable(str(last_error)) from last_error
        raise PrefixCacheUnavailable(f"prefix cache handle has no available pools: {self.cache_id}")


class PrefixCacheRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, PrefixCacheHandle] = {}

    def register(self, handle: PrefixCacheHandle) -> None:
        with self._lock:
            self._handles[str(handle.cache_id)] = handle
            logger.debug(
                "prefix cache handle registered cache_id=%s prefix_len=%s pools=%s metadata=%s",
                handle.cache_id,
                handle.prefix_len,
                len(handle.pools),
                handle.metadata,
            )

    def get(self, cache_id: str) -> PrefixCacheHandle | None:
        with self._lock:
            handle = self._handles.get(str(cache_id))
            logger.debug("prefix cache handle lookup cache_id=%s hit=%s", cache_id, handle is not None)
            return handle

    def __len__(self) -> int:
        with self._lock:
            return len(self._handles)


__all__ = [
    "PrefixCacheEntry",
    "PrefixCacheHandle",
    "PrefixCacheRegistry",
    "PrefixCacheReplica",
    "PrefixStaticCachePool",
    "PrefixStaticCacheSlot",
    "RequestCacheState",
]
