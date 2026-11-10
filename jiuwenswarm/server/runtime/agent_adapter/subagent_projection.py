# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consumer-owned subagent projection and history, independent of SDK parsing."""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


_SUBAGENT_KINDS = ("subagent_updated", "subagent_message", "subagent_activity")


@dataclass(frozen=True)
class SubagentStreamEvent:
    """A neutral SDK event. Constructing it never projects or writes history."""

    kind: str
    projection: dict[str, Any]
    parent_session_id: str = ""


@dataclass
class _ParentTransaction:
    lock: Any = field(default_factory=threading.RLock)
    users: int = 0


def parse_subagent_event(
    chunk: Any, *, parent_session_id: str | None = None,
) -> SubagentStreamEvent | None:
    """Copy the three SDK envelopes without mutating their payload or state."""
    kind = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
    kind = getattr(kind, "value", kind)
    if kind not in _SUBAGENT_KINDS:
        return None
    payload = chunk.get("payload") if isinstance(chunk, dict) else getattr(chunk, "payload", None)
    projection = payload.get(kind) if isinstance(payload, dict) else None
    if not isinstance(projection, dict):
        return None
    return SubagentStreamEvent(kind, dict(projection), str(parent_session_id or ""))


class SubagentProjectionService:
    """Own Web roster state and ordered commits through explicit history ports.

    One process owner preserves the existing parent-scoped progress indexes
    across foreground, control, Team and repair consumers. There are no tasks
    or queues: each accepted event commits before its consumer can continue.
    """

    def __init__(
        self, *, write_history: Callable[..., None], clock: Callable[[], float] = time.time,
    ) -> None:
        self._write_history = write_history
        self._clock = clock
        self._progress_batches: dict[str, list[str]] = {}
        self._parents: dict[str, _ParentTransaction] = {}
        self._registry_lock = threading.Lock()

    @contextmanager
    def _transaction(self, parent_session_id: str) -> Iterator[None]:
        # The registry lock only protects short bookkeeping. Slow disk work is
        # serialized for this parent, so unrelated Sessions remain independent.
        with self._registry_lock:
            transaction = self._parents.setdefault(parent_session_id, _ParentTransaction())
            transaction.users += 1
        try:
            with transaction.lock:
                yield
        finally:
            with self._registry_lock:
                transaction.users -= 1
                if transaction.users == 0 and parent_session_id not in self._progress_batches:
                    self._parents.pop(parent_session_id, None)

    def clear_parent(self, parent_session_id: str | None) -> None:
        if parent_session_id:
            with self._transaction(parent_session_id):
                self._progress_batches.pop(parent_session_id, None)

    async def clear_parent_async(self, parent_session_id: str | None) -> None:
        """Wait for this parent's accepted commit without blocking the loop."""
        from jiuwenswarm.server.runtime.session.history_io import run_history_io

        await run_history_io(self.clear_parent, parent_session_id)

    def clear(self) -> None:
        with self._registry_lock:
            parents = list(self._parents)
        for parent_session_id in parents:
            self.clear_parent(parent_session_id)

    @staticmethod
    def legacy_status(projection: dict[str, Any]) -> tuple[str, str]:
        status = str(projection.get("status") or "running")
        failed = (
            status == "closed" and projection.get("closed_reason") == "failed"
        ) or (status == "idle" and projection.get("turn_outcome") == "failed")
        if failed:
            error = projection.get("error")
            return "error", str(error.get("message") or "") if isinstance(error, dict) else ""
        return ("completed", "") if status in ("closed", "idle") else ("starting", "")

    def parallel_fields(
        self, *, parent_session_id: str, subagent_id: str, legacy_status: str,
    ) -> tuple[int, int, bool]:
        if not parent_session_id or not subagent_id:
            return 0, 1, False
        with self._transaction(parent_session_id):
            order = self._progress_batches.setdefault(parent_session_id, [])
            if legacy_status in ("completed", "error"):
                if subagent_id not in order:
                    if not order:
                        self._progress_batches.pop(parent_session_id, None)
                    return 0, 1, False
                index, total = order.index(subagent_id), max(len(order), 1)
                order.remove(subagent_id)
                if not order:
                    self._progress_batches.pop(parent_session_id, None)
                return index, total, total > 1
            if subagent_id not in order:
                order.append(subagent_id)
            return order.index(subagent_id), len(order), len(order) > 1

    def project_roster(self, projection: dict[str, Any]) -> dict[str, Any]:
        subagent_id = str(projection.get("subagent_id") or "")
        description = (
            str(projection.get("display_name") or "").strip()
            or str(projection.get("task_description") or "").strip()
            or subagent_id
        )
        legacy_status, message = self.legacy_status(projection)
        index, total, is_parallel = self.parallel_fields(
            parent_session_id=str(projection.get("parent_session_id") or ""),
            subagent_id=subagent_id,
            legacy_status=legacy_status,
        )
        payload = {
            "event_type": "chat.subtask_update", **projection, "task_id": subagent_id,
            "description": description, "content": description, "legacy_status": legacy_status,
            "index": index, "total": total, "is_parallel": is_parallel,
        }
        if message:
            payload["message"] = message
        parent_session_id = str(projection.get("parent_session_id") or "")
        if parent_session_id:
            from jiuwenswarm.server.runtime.session.session_token_usage import get_session_token_usage

            usage = get_session_token_usage(parent_session_id)
            if usage.get("revision", 0) > 0:
                payload["session_usage"] = usage
        return payload

    @staticmethod
    def _scope(projection: dict[str, Any]) -> tuple[str, str]:
        return (
            str(projection.get("parent_session_id") or "").strip(),
            str(projection.get("subagent_id") or "").strip(),
        )

    def persist_transcript(self, projection: dict[str, Any]) -> None:
        parent_session_id, subagent_id = self._scope(projection)
        if not parent_session_id or not subagent_id:
            return
        seq = projection.get("seq")
        role = str(projection.get("role") or "assistant")
        extra: dict[str, Any] = {}
        reasoning = projection.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            extra["reasoning_content"] = reasoning.strip()
        try:
            phase_id = int(projection.get("phase_id") or 0)
        except (TypeError, ValueError):
            phase_id = 0
        if phase_id > 0:
            extra["phase_id"] = phase_id
        if isinstance(projection.get("extra"), dict):
            extra.update(projection["extra"])
        extra["parent_session_id"] = parent_session_id
        at_ms = projection.get("at_ms")
        self._write_history(
            session_id=parent_session_id, subagent_id=subagent_id,
            request_id=f"{subagent_id}:{seq}" if seq is not None else subagent_id,
            channel_id="subagent", role=role, content=str(projection.get("content") or ""),
            timestamp=float(at_ms) / 1000 if at_ms else self._clock(),
            event_type=(str(projection.get("event_type") or "").strip() or None)
            if role == "assistant" else None,
            extra=extra, mode="subagent",
        )

    def persist_roster(self, projection: dict[str, Any], web_payload: dict[str, Any]) -> None:
        parent_session_id, subagent_id = self._scope(projection)
        if not parent_session_id or not subagent_id:
            return
        at_ms = projection.get("updated_at_ms") or projection.get("created_at_ms")
        revision = projection.get("revision")
        request_id = f"subagent-roster-{subagent_id}"
        if revision is not None:
            request_id += f":{revision}"
        self._write_history(
            session_id=parent_session_id, subagent_id=subagent_id, request_id=request_id,
            channel_id="subagent", role="assistant", event_type="chat.subtask_update",
            content=str(web_payload.get("description") or subagent_id),
            timestamp=float(at_ms) / 1000 if at_ms else self._clock(),
            extra=web_payload, mode="subagent",
        )

    def persist_activity(self, projection: dict[str, Any]) -> None:
        parent_session_id, subagent_id = self._scope(projection)
        if not parent_session_id or not subagent_id:
            return
        task_id = str(projection.get("task_id") or "").strip() or "turn"
        seq = projection.get("seq")
        seq_part = str(seq) if seq is not None else str(projection.get("at_ms") or "0")
        at_ms = projection.get("at_ms")
        self._write_history(
            session_id=parent_session_id, subagent_id=subagent_id,
            request_id=f"{subagent_id}:activity:{task_id}:{seq_part}", channel_id="subagent",
            role="assistant", content=str(projection.get("summary") or ""),
            timestamp=float(at_ms) / 1000 if at_ms else self._clock(),
            event_type="chat.subagent_activity",
            extra={"subagent_activity": {**projection, "parent_session_id": parent_session_id}},
            mode="subagent",
        )

    def consume(self, event: SubagentStreamEvent) -> dict[str, Any] | None:
        """Commit before returning visible output; transcript remains silent."""
        projection = dict(event.projection)
        parent_session_id = str(projection.get("parent_session_id") or event.parent_session_id).strip()
        if parent_session_id:
            projection["parent_session_id"] = parent_session_id
        with self._transaction(parent_session_id):
            if event.kind == "subagent_updated":
                previous = self._progress_batches.get(parent_session_id)
                previous = list(previous) if previous is not None else None
                try:
                    payload = self.project_roster(projection)
                    self.persist_roster(projection, payload)
                except BaseException:
                    # Publish roster changes only with a successful record.
                    # A failed completion must remain in the parallel batch so
                    # retrying the same revision preserves its index/total.
                    if previous is None:
                        self._progress_batches.pop(parent_session_id, None)
                    else:
                        self._progress_batches[parent_session_id] = previous
                    raise
                return payload
            if event.kind == "subagent_message":
                self.persist_transcript(projection)
                return None
            if event.kind == "subagent_activity":
                self.persist_activity(projection)
                return {"event_type": "chat.subagent_activity", **event.projection}
        raise ValueError(f"Unsupported subagent event: {event.kind}")


def _append_history(**kwargs: Any) -> None:
    # Resolve at the history port, without importing the SDK or a facade.
    from jiuwenswarm.server.runtime.session.session_history import append_history_record

    append_history_record(wait_for_write=True, **kwargs)


SUBAGENT_PROJECTION = SubagentProjectionService(write_history=_append_history)


async def project_stream_event(
    parsed: Any, *, owner: SubagentProjectionService | None = None,
    parent_session_id: str | None = None,
) -> Any:
    """Explicit asynchronous consumer boundary, including silent transcripts."""
    if not isinstance(parsed, SubagentStreamEvent):
        return parsed
    from jiuwenswarm.server.runtime.session.history_io import run_history_io

    if parent_session_id and not parsed.parent_session_id:
        parsed = SubagentStreamEvent(parsed.kind, parsed.projection, parent_session_id)
    return await run_history_io((owner or SUBAGENT_PROJECTION).consume, parsed)
