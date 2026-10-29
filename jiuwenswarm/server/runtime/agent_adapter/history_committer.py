# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ordered history commits and durable buffers belonging to one request stream."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import USER_INTERACTION_EVENTS

import asyncio
import copy
import time
from typing import Any, Awaitable, Callable


class HistoryCommitter:
    """Own pending output until its final, interrupt or input boundary is durable.

    The consumer awaits each write before yielding the corresponding boundary.
    Record and metadata ports keep this component independent from SDK and
    transport implementations while retaining the host's history policy.
    """

    def __init__(
        self,
        *,
        params: dict[str, Any],
        session_id: str,
        request_id: str,
        channel_id: str,
        write_record: Callable[..., Awaitable[None]],
        decorate_web: Callable,
        decorate_heartbeat: Callable,
        decorate_cross_session: Callable,
        resolve_final_timestamp: Callable,
        inherit_request_source: bool = True,
    ) -> None:
        self.params = params
        self.session_id = session_id
        self.request_id = request_id
        self.channel_id = channel_id
        self._write_record = write_record
        self._decorate_web = decorate_web
        self._decorate_heartbeat = decorate_heartbeat
        self._decorate_cross_session = decorate_cross_session
        self._resolve_final_timestamp = resolve_final_timestamp
        self._inherit_request_source = inherit_request_source
        self._pending_write = None
        self.pending_final_chunks: list[str] = []
        self.pending_final_started_at: float | None = None
        self.pending_final_order: dict[str, Any] | None = None
        self.pending_reasoning_chunks: list[str] = []
        self.pending_reasoning_started_at: float | None = None
        self.pending_reasoning_updated_at: float | None = None
        self.pending_reasoning_order: dict[str, Any] | None = None
        self.final_content = ""
        self.output_phase_metadata: dict[str, Any] = {}
        self.saw_goal_stream_output = False
        self._team_member_history = None

    async def persist_team_member_pending(self, *, completed_at: float | None = None) -> None:
        """Drain member buckets independently, retaining failed writes for retry."""
        await self.retry_pending_writes()
        history = self._team_member_history
        if history is None:
            return
        for key in history.member_keys():
            snapshot = copy.deepcopy(self._team_member_history)
            pending = snapshot.take_pending_final(key, completed_at=completed_at)
            if pending is None:
                self._team_member_history = snapshot
                continue
            extra = self._decorate_record("chat.final", dict(pending.extra), None)
            record = self._assistant_record("chat.final", pending.content, extra, pending.started_at)
            await self._commit_record(record, lambda value=snapshot: setattr(self, "_team_member_history", value))

    async def handle_team_member_event(self, payload: dict[str, Any]) -> bool:
        """Keep teammate deltas and reasoning out of the leader's durable buffers."""
        from jiuwenswarm.server.runtime.agent_adapter.team_member_stream_history import (
            MEMBER_SEGMENT_BOUNDARY_EVENTS, TeamMemberStreamHistory, team_member_stream_key,
        )

        key = team_member_stream_key(payload)
        if key is None:
            return False
        await self.retry_pending_writes()
        if self._team_member_history is None:
            self._team_member_history = TeamMemberStreamHistory()
        history = self._team_member_history
        event = str(payload.get("event_type") or "")
        content = str(payload.get("content") or "")
        if event == "chat.delta":
            history.note_delta(key, content, payload)
            return True
        if event == "chat.reasoning":
            history.note_reasoning(key, content, payload)
            return True
        if event in MEMBER_SEGMENT_BOUNDARY_EVENTS or (event == "chat.final" and not content):
            snapshot = copy.deepcopy(history)
            pending = snapshot.take_pending_final(key)
            if pending is not None:
                extra = self._decorate_record("chat.final", dict(pending.extra), payload)
                record = self._assistant_record("chat.final", pending.content, extra, pending.started_at)
                await self._commit_record(record, lambda: setattr(self, "_team_member_history", snapshot))
                history = self._team_member_history
        if event in USER_INTERACTION_EVENTS:
            snapshot = copy.deepcopy(self._team_member_history)
            pending = snapshot.take_pending_reasoning(key)
            if pending is not None:
                started_at, extra = pending
                record = self._assistant_record("chat.reasoning", "", self._decorate_record("chat.reasoning", extra))
                record["timestamp"] = (started_at or time.time() * 1000) / 1000
                await self._commit_record(record, lambda: setattr(self, "_team_member_history", snapshot))
                history = self._team_member_history
        if not (event.startswith("chat.") or event == "context.usage" or event in USER_INTERACTION_EVENTS):
            return True
        snapshot = copy.deepcopy(history)
        started_at, order = snapshot.text_segment_start(key)
        extra = {name: value for name, value in payload.items() if name not in {"event_type", "content"}}
        if event in {"chat.final", "chat.tool_call"}:
            extra = snapshot.attach_reasoning(key, extra) or extra
        if event == "chat.final":
            if order:
                extra["output_order"] = order
            snapshot.discard_text(key, final_content=content)
        record = self._assistant_record(
            event, payload.get("content") or payload.get("error") or "",
            self._decorate_record(event, extra, payload), started_at if event == "chat.final" else None,
        )
        await self._commit_record(record, lambda: setattr(self, "_team_member_history", snapshot))
        return True

    def note_durable_reasoning_delta(self, payload: dict[str, Any]) -> None:
        if self.pending_reasoning_started_at is None:
            self.pending_reasoning_order = payload.get("output_order")
        now_ms = time.time() * 1000
        if self.pending_reasoning_started_at is None:
            self.pending_reasoning_started_at = now_ms
        self.pending_reasoning_updated_at = now_ms

    def consume_durable_reasoning_content(
        self,
    ) -> tuple[str, float | None, float | None]:
        reasoning_text = "".join(self.pending_reasoning_chunks)
        started_at = self.pending_reasoning_started_at
        updated_at = self.pending_reasoning_updated_at
        self.pending_reasoning_chunks = []
        self.pending_reasoning_started_at = None
        self.pending_reasoning_updated_at = None
        return (reasoning_text, started_at, updated_at)

    def attach_reasoning_content(
        self, extra_fields: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        reasoning_text, started_at, updated_at = (
            self.consume_durable_reasoning_content()
        )
        if not reasoning_text.strip():
            return extra_fields
        merged = dict(extra_fields) if isinstance(extra_fields, dict) else {}
        merged["reasoning_content"] = reasoning_text
        if self.pending_reasoning_order:
            merged["reasoning_output_order"] = self.pending_reasoning_order
        merged["reasoning_updated_at"] = updated_at or started_at or time.time() * 1000
        return merged

    async def retry_pending_writes(self) -> None:
        """Keep a failed record and its state transition owned until a retry."""
        if self._pending_write is None:
            return

        async def commit():
            record, accepted = self._pending_write
            await self._write_record(**record)
            accepted()
            self._pending_write = None

        await self._drain_accepted(commit())

    @staticmethod
    async def _drain_accepted(operation):
        boundary = asyncio.create_task(operation)
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(boundary)
                break
            except asyncio.CancelledError as exc:
                if boundary.cancelled():
                    raise
                cancelled = exc
        if cancelled is not None:
            raise cancelled
        return result

    async def _commit_record(self, record, accepted=lambda: None):
        await self.retry_pending_writes()
        self._pending_write = (record, accepted)
        await self.retry_pending_writes()

    def _reasoning_snapshot(self, extra_fields=None):
        chunks = list(self.pending_reasoning_chunks)
        extra = dict(extra_fields or {})
        if "".join(chunks).strip():
            extra["reasoning_content"] = "".join(chunks)
            if self.pending_reasoning_order:
                extra["reasoning_output_order"] = self.pending_reasoning_order
            extra["reasoning_updated_at"] = (
                self.pending_reasoning_updated_at
                or self.pending_reasoning_started_at
                or time.time() * 1000
            )
        return chunks, extra

    def _accept_reasoning(self, chunks):
        del self.pending_reasoning_chunks[:len(chunks)]
        if not self.pending_reasoning_chunks:
            self.pending_reasoning_started_at = None
            self.pending_reasoning_updated_at = None
            self.pending_reasoning_order = None

    def _decorate_record(self, event_type, extra_fields=None, payload=None):
        return self._decorate_cross_session(
            self._decorate_heartbeat(
                self._decorate_web(
                    dict(extra_fields or {}), self.params, self.channel_id,
                    event_type=event_type, **({"payload": payload} if payload is not None else {}),
                ), self.params,
            ), self.params,
        ) or {}

    def _assistant_record(self, event_type, content, extra, started_at=None):
        return dict(
            session_id=self.session_id, request_id=self.request_id,
            channel_id=self.channel_id, role="assistant", event_type=event_type,
            content=content,
            timestamp=self._resolve_final_timestamp(
                event_type=event_type, segment_started_at=started_at, extra_fields=extra,
            ),
            extra=extra or None, mode=self.params.get("mode", "unknown"),
        )

    async def persist_pending_reasoning(self) -> None:
        """Persist unfinished reasoning; failed writes retain the same buffer."""
        await self.retry_pending_writes()
        chunks, extra = self._reasoning_snapshot(self.output_phase_metadata)
        if not "".join(chunks).strip():
            return
        started_at = self.pending_reasoning_started_at
        record = self._assistant_record(
            "chat.reasoning", "", self._decorate_record("chat.reasoning", extra),
        )
        record["timestamp"] = (started_at or time.time() * 1000) / 1000
        await self._commit_record(record, lambda: self._accept_reasoning(chunks))

    def reset_durable_pending_final(self) -> None:
        self.pending_final_order = None
        self.pending_final_chunks = []
        self.pending_final_started_at = None

    def note_durable_pending_final_delta(
        self, content: str, payload: dict[str, Any]
    ) -> None:
        if self.pending_final_started_at is None:
            self.pending_final_started_at = time.time()
            self.pending_final_order = payload.get("output_order")
        self.pending_final_chunks.append(content)

    def note_goal_stream_payload(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        if self.saw_goal_stream_output:
            return
        if event_type.startswith("goal.") or payload.get("goal_intermediate"):
            self.saw_goal_stream_output = True

    async def persist_pending_final_text(
        self, *, completed_at: float | None = None
    ) -> None:
        await self.retry_pending_writes()
        text = "".join(self.pending_final_chunks)
        if not text or text == self.final_content:
            self.reset_durable_pending_final()
            return
        extra = {
            **self.output_phase_metadata,
            **({"output_order": self.pending_final_order} if self.pending_final_order else {}),
            **({"completed_at": completed_at} if completed_at is not None else {}),
        }
        if self._inherit_request_source:
            extra.update({
                key: value for key, value in self.params.items()
                if key in ("source", "proactive_type", "proactive_target", "automation", "proactive_rec_id")
            })
        await self._persist_final(text, extra)

    async def _persist_final(self, text, extra, payload=None):
        pending_count = len(self.pending_final_chunks)
        reasoning, extra = self._reasoning_snapshot(extra)
        record = self._assistant_record(
            "chat.final", text, self._decorate_record("chat.final", extra, payload),
            self.pending_final_started_at,
        )

        def accepted():
            del self.pending_final_chunks[:pending_count]
            if not self.pending_final_chunks:
                self.pending_final_started_at = None
                self.pending_final_order = None
            self._accept_reasoning(reasoning)
            self.final_content = text

        await self._commit_record(record, accepted)

    async def commit_assistant_event(self, payload: dict[str, Any]) -> None:
        """Commit continuation events using the same final/reasoning state owner.

        A resume envelope intentionally omits its ask-user source from output
        metadata. It otherwise shares ordered buffers, timestamps, retries and
        accepted-write cancellation barriers with an ordinary request stream.
        """
        await self.retry_pending_writes()
        if await self.handle_team_member_event(payload):
            return
        event_type = payload.get("event_type")
        if not isinstance(event_type, str):
            return
        self.note_goal_stream_payload(event_type, payload)
        content = str(payload.get("content") or "")
        if event_type == "chat.delta":
            if content:
                self.note_durable_pending_final_delta(content, payload)
            return
        if event_type == "chat.reasoning":
            if content:
                self.note_durable_reasoning_delta(payload)
                self.pending_reasoning_chunks.append(content)
            return
        if not (event_type.startswith("chat.") or event_type == "context.usage"):
            return
        if event_type == "chat.final":
            if not content:
                await self.persist_pending_final_text()
                await self.persist_pending_reasoning()
            elif content != self.final_content or self.pending_final_chunks:
                extra = {key: value for key, value in payload.items() if key not in ("event_type", "content")}
                if self.pending_final_order:
                    extra["output_order"] = self.pending_final_order
                await self._persist_final(content, extra, payload)
            return
        boundary = event_type in ("chat.tool_call", "chat.error", "chat.done") or event_type in USER_INTERACTION_EVENTS
        if boundary:
            await self.persist_pending_final_text()
            await self.persist_pending_reasoning()
        extra = {key: value for key, value in payload.items() if key not in ("event_type", "content")}
        record = self._assistant_record(
            event_type, payload.get("content") or payload.get("error") or "",
            self._decorate_record(event_type, extra, payload),
        )

        def accepted():
            if boundary:
                self.final_content = ""

        await self._commit_record(record, accepted)

    async def persist_input_boundary(self, payload: dict[str, Any]) -> None:
        """Drain the whole accepted-input boundary before propagating cancellation.

        Shielding individual writes only protects the old assistant record: a
        cancelled caller would then skip the accepted user input. Keep their
        ordered sequence together, including repeated cancellation requests.
        """
        await self._drain_accepted(self._commit_input_boundary(payload))

    async def _commit_input_boundary(self, payload: dict[str, Any]) -> None:
        await self.persist_pending_final_text(completed_at=payload["timestamp"] / 1000)
        await self.persist_pending_reasoning()
        await self.persist_team_member_pending(completed_at=payload["timestamp"] / 1000)
        await self._write_record(
            session_id=self.session_id,
            request_id=payload["input_request_id"],
            channel_id=self.channel_id,
            role="user",
            content=payload["content"],
            timestamp=payload["timestamp"] / 1000,
            extra={
                "output_order": payload.get("output_order"),
                "is_supplemental_input": True,
                **{
                    key: payload[key]
                    for key in ("message_origin", "session_message_id", "cross_session")
                    if key in payload
                },
            },
            mode=self.params.get("mode", "unknown"),
        )
