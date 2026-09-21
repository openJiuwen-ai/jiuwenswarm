# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral events emitted by the shared Agent Runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class RuntimeEvent:
    """One event in the Runtime stream before any output/wire rendering."""

    request_id: str
    channel_id: str
    session_id: str | None
    payload: dict[str, Any] | None
    agent_ref: Any = None
    metadata: dict[str, Any] | None = None
    is_complete: bool = False
    ok: bool = True
    runtime_completion: str | None = None

    @property
    def event_type(self) -> str:
        if not isinstance(self.payload, dict):
            return ""
        return str(self.payload.get("event_type") or "")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # Execution-only state must not change existing JSON/wire renderers.
        value.pop("runtime_completion", None)
        return value

    @classmethod
    def from_agent_message(
        cls,
        message: Any,
        *,
        request_id: str,
        channel_id: str,
        session_id: str | None,
        default_agent_ref: Any = None,
        default_complete: bool = False,
    ) -> RuntimeEvent:
        payload = getattr(message, "payload", None)
        if payload is None:
            event_payload = None
        elif isinstance(payload, dict):
            event_payload = dict(payload)
        else:
            event_payload = {
                "event_type": "chat.final",
                "content": str(payload or ""),
            }
        return cls(
            request_id=str(getattr(message, "request_id", None) or request_id),
            channel_id=str(getattr(message, "channel_id", None) or channel_id),
            session_id=session_id,
            payload=event_payload,
            agent_ref=(
                getattr(message, "agent_ref", None)
                if getattr(message, "agent_ref", None) is not None
                else default_agent_ref
            ),
            metadata=(
                dict(message.metadata)
                if isinstance(getattr(message, "metadata", None), dict)
                else None
            ),
            is_complete=bool(getattr(message, "is_complete", default_complete)),
            ok=bool(getattr(message, "ok", True)),
            runtime_completion=getattr(message, "runtime_completion", None),
        )

    @classmethod
    def control(
        cls,
        *,
        request_id: str,
        channel_id: str,
        session_id: str | None,
        payload: dict[str, Any],
    ) -> RuntimeEvent:
        return cls(
            request_id=request_id,
            channel_id=channel_id,
            session_id=session_id,
            payload=payload,
        )

    @classmethod
    def error(
        cls,
        *,
        request_id: str,
        channel_id: str,
        session_id: str | None,
        error: BaseException,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        return cls(
            request_id=request_id,
            channel_id=channel_id,
            session_id=session_id,
            payload={"event_type": "runtime.error", "error": str(error)},
            metadata=dict(metadata) if metadata is not None else None,
            is_complete=True,
            ok=False,
        )


# Terminal error event types: a model/agent/runtime failure surfaced as a
# stream event rather than a raised exception. Such events can arrive with
# ``ok=True`` because ``RuntimeEvent.from_agent_message`` defaults ``ok`` to
# True, so consumers must inspect ``event_type`` in addition to ``event.ok``.
# Relying on ``event.ok`` alone misclassifies failed runs as succeeded (the
# bug behind 心跳任务 showing 「上次运行成功」 while every model call failed).
# Shared so every consumer -- AgentServer heartbeat/session-message executors,
# cron scheduler, runtime turn confirmation -- agrees on the set, and adding a
# new terminal error type is a one-place change.
TERMINAL_ERROR_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "chat.error",
        "runtime.error",
        "execution.error",
        "team.error",
        "error",
    }
)


__all__ = ["RuntimeEvent", "TERMINAL_ERROR_EVENT_TYPES"]
