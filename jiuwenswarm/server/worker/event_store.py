# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded replay owned by a resident Worker, independent of IPC sessions.

History remains the durable conversation store. This journal bridges transport
restarts; when a cursor expires clients reload history and the execution snapshot.
It never resubmits a request or replays a tool operation.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.ipc.protocol import request_scope


@dataclass
class DispatchRecord:
    dispatch_id: str
    request: AgentRequest
    scope: tuple[str, ...]
    active: bool = True
    last_sequence: int = 0
    last_execution_id: str = ""
    is_processing: bool = False
    entered_at_ns: int | None = None
    interactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminal: dict[str, Any] | None = None


class WorkerEventStore:
    def __init__(self, *, max_events: int = 2048, max_bytes: int = 8 * 1024 * 1024) -> None:
        self.instance = uuid.uuid4().hex
        self._sequence = 0
        self._frames: deque[tuple[tuple[str, ...], dict[str, Any], int]] = deque()
        self._bytes = 0
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._expired_sequence = 0
        self.records: OrderedDict[str, DispatchRecord] = OrderedDict()
        self._active: dict[tuple[str, ...], str] = {}

    def register(self, request: AgentRequest, dispatch_id: str) -> DispatchRecord:
        scope = request_scope(request)
        key = (*scope, str(request.request_id or ""))
        if key in self._active:
            raise ValueError("duplicate active request_id")
        if len(self._active) >= 256:
            raise ValueError("worker dispatch limit reached")
        if len(self.records) >= 2048:
            raise ValueError("worker retained interaction limit reached")
        record = DispatchRecord(dispatch_id, request, scope)
        self.records[dispatch_id] = record
        self._active[key] = dispatch_id
        return record

    def finish(self, dispatch_id: str) -> None:
        record = self.records.get(dispatch_id)
        if record is None:
            return
        record.active = False
        key = (*record.scope, str(record.request.request_id or ""))
        if not record.is_processing and not record.interactions and self._active.get(key) == dispatch_id:
            del self._active[key]
        while len(self.records) > 512:
            removable = next((key for key, value in self.records.items()
                              if not value.active and not value.interactions), None)
            if removable is None:
                break
            del self.records[removable]

    def complete(self, dispatch_id: str, *, state: str, error: str = "") -> None:
        record = self.records.get(dispatch_id)
        if record is None:
            return
        record.is_processing = False
        record.interactions.clear()
        record.terminal = {"event_type": "runtime.completed", "state": state, "error": error}
        self.finish(dispatch_id)

    def append(self, wire: dict[str, Any], dispatch_id: str) -> dict[str, Any]:
        self._sequence += 1
        record = self.records.get(dispatch_id)
        scope = record.scope if record else ("", "", "", "null", str(wire.get("session_id") or ""))
        payload = copy.deepcopy(wire)
        if record and record.scope[-1]:
            # A restarted Gateway has no original request-to-session map.
            # Carry the resolved conversation from the accepted request on
            # every live and replayed frame, rather than trusting event text.
            payload["session_id"] = record.scope[-1]
        metadata = dict(payload.get("metadata") or {})
        if record and record.scope[2]:
            metadata["app_id"] = record.scope[2]
        metadata["runtime_event"] = {
            "worker_instance": self.instance,
            "sequence": self._sequence,
            "dispatch_id": dispatch_id,
        }
        payload["metadata"] = metadata
        frame = {
            "request_id": str(wire.get("request_id") or ""),
            "dispatch_id": dispatch_id,
            "sequence": self._sequence,
            "payload": payload,
        }
        size = len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
        self._frames.append((scope, frame, size))
        self._bytes += size
        while self._frames and (len(self._frames) > self._max_events or self._bytes > self._max_bytes):
            _expired_scope, expired, expired_size = self._frames.popleft()
            self._expired_sequence = expired["sequence"]
            self._bytes -= expired_size
        if record:
            body = wire.get("body") or wire.get("payload") or {}
            if isinstance(body, dict):
                # E2A custom chunks and terminal results wrap the native event.
                nested = body.get("delta") if isinstance(body.get("delta"), dict) else body.get("result")
                if isinstance(nested, dict):
                    body = {**body, **nested}
                record.last_execution_id = str(body.get("execution_id") or record.last_execution_id)
                if "is_processing" in body:
                    record.is_processing = bool(body["is_processing"])
                event = str(body.get("event_type") or wire.get("response_kind") or "")
                interaction = str(body.get("interaction_id") or body.get("control_id")
                                  or (body.get("request_id") if "ask_user" in event else "") or "")
                if interaction and any(kind in event for kind in ("ask_user", "permission", "approval", "interrupted")):
                    record.interactions[interaction] = copy.deepcopy(body)
                    record.is_processing = True
                # A suspended interaction emits chat.final for the output
                # segment. Only execution completion clears its pending input.
                if ("runtime.completed" in event or "chat.error" in event
                        or ("chat.final" in event and body.get("final_mode") != "patch_segment")):
                    record.terminal = copy.deepcopy(body)
                    record.is_processing = False
                    record.interactions.clear()
                    if not record.active:
                        self.finish(dispatch_id)
            record.last_sequence = self._sequence
        return frame

    def snapshot(self, request: AgentRequest, *, after: int = 0) -> dict[str, Any]:
        scope = request_scope(request)
        records = [record for record in self.records.values() if record.scope == scope]
        return {
            "worker_instance": self.instance,
            "cursor": self._sequence,
            "history_required": after < self._expired_sequence,
            "is_processing": any(record.active or record.is_processing for record in records),
            "dispatches": [{
                "dispatch_id": record.dispatch_id,
                "request_id": record.request.request_id,
                "active": record.active,
                "phase": ("waiting_for_control" if record.interactions else
                          "completed" if record.terminal else "executing" if record.entered_at_ns
                          else "accepted" if record.active else "handler_returned"),
                "execution_id": record.last_execution_id,
                "entered_at_ns": record.entered_at_ns,
                "pending_interactions": list(record.interactions.values()),
                "terminal": record.terminal,
            } for record in records],
        }

    def replay(self, request: AgentRequest, *, after: int) -> list[dict[str, Any]]:
        scope = request_scope(request)
        return [copy.deepcopy(frame) for owner, frame, _ in self._frames
                if owner == scope and frame["sequence"] > after]
