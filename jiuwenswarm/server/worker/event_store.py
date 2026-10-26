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
from dataclasses import dataclass, field, replace
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.session_message import SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY
from jiuwenswarm.runtime.session.model import TEAM_ATTACH_ONLY_PARAM
from jiuwenswarm.server.ipc.protocol import request_scope


_SESSION_INSPECTION_METHODS = frozenset({
    ReqMethod.TEAM_LIST,
    ReqMethod.ORG_SNAPSHOT,
    ReqMethod.TEAM_SNAPSHOT,
    ReqMethod.TEAM_MEMBERS_GET,
    ReqMethod.TEAM_HISTORY_GET,
    ReqMethod.SESSION_ASSETS_LIST,
    ReqMethod.SESSION_PLAN_STATUS,
    ReqMethod.COMMAND_WORKFLOWS,
    ReqMethod.A4P_AUTHORIZATION_PENDING,
})


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

    @property
    def is_passive_subscription(self) -> bool:
        """A Team observer receives output but does not submit execution work."""
        return (
            self.request.req_method == ReqMethod.CHAT_SEND
            and self.request.params.get(TEAM_ATTACH_ONLY_PARAM) is True
        )

    @property
    def has_pending_execution(self) -> bool:
        """Retain queued work without treating inspection RPC delivery as execution."""
        if not self.active or self.is_passive_subscription:
            return False
        if self.request.req_method in _SESSION_INSPECTION_METHODS:
            return False
        if self.request.req_method == ReqMethod.COMMAND_GOAL:
            action = str(self.request.params.get("action") or "get").strip().lower()
            return action != "get"
        return True


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
        # Active IDs belong to the caller; changing Session cannot bypass the
        # duplicate guard. Journal lookup and replay remain Session-scoped.
        key = (*scope[:4], str(request.request_id or ""))
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
        key = (*record.scope[:4], str(record.request.request_id or ""))
        if not record.is_processing and not record.interactions and self._active.get(key) == dispatch_id:
            del self._active[key]
        while len(self.records) > 512:
            removable = next((key for key, value in self.records.items()
                              if not value.active and not value.is_processing and not value.interactions), None)
            if removable is None:
                break
            del self.records[removable]

    def internal_record(self, request: AgentRequest) -> DispatchRecord:
        """Track an authorized server execution, including mailbox background work."""
        scope = request_scope(request)
        existing = next((record for record in reversed(self.records.values())
                         if record.scope == scope and record.request.request_id == request.request_id), None)
        if existing is not None:
            if existing.active or existing.is_processing:
                self._active[(*scope[:4], str(request.request_id or ""))] = existing.dispatch_id
            return existing
        return self.register(request, uuid.uuid4().hex)

    def complete(self, dispatch_id: str, *, state: str, error: str = "") -> None:
        record = self.records.get(dispatch_id)
        if record is None:
            return
        record.is_processing = False
        record.interactions.clear()
        record.terminal = {"event_type": "runtime.completed", "state": state, "error": error}
        self.finish(dispatch_id)

    @staticmethod
    def _push_request(wire: dict[str, Any], record: DispatchRecord | None) -> AgentRequest | None:
        """Resolve a server push target before acquiring its publication lock."""
        metadata = wire.get("metadata") or {}
        target = str(wire.get("session_id") or "")
        if metadata.get(E2A_WIRE_SERVER_PUSH_KEY) and target:
            if record is not None and target == record.scope[-1]:
                return None
            # This is an authorized AgentServer push, often produced by a
            # mailbox task inheriting the source request's ContextVars. Its
            # target must own the journal and processing state, not the source.
            owner = metadata.get(SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY)
            return replace(record.request, session_id=target,
                              request_id=str(wire.get("request_id") or ""),
                              channel_id=str(wire.get("channel") or record.request.channel_id),
                              params={"session_id": target},
                              user_id=str(owner or record.request.user_id),
                              metadata={**(record.request.metadata or {}), **metadata}) if record else AgentRequest(
                                  request_id=str(wire.get("request_id") or ""),
                                  session_id=target, channel_id=str(wire.get("channel") or ""),
                                  user_id=str(owner or metadata.get("user_id") or ""), metadata=metadata,
                                  params={"session_id": target})
        return None

    def publication_scope(self, wire: dict[str, Any], dispatch_id: str) -> tuple[str, ...]:
        record = self.records.get(dispatch_id)
        target = self._push_request(wire, record)
        if target is not None:
            return request_scope(target)
        return record.scope if record else ("", "", "", "null", str(wire.get("session_id") or ""))

    def append(self, wire: dict[str, Any], dispatch_id: str) -> dict[str, Any]:
        record = self.records.get(dispatch_id)
        target = self._push_request(wire, record)
        if target is not None:
            record = self.internal_record(target)
            dispatch_id = record.dispatch_id
            self.finish(dispatch_id)
        self._sequence += 1
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
                final_segment = "chat.final" in event and body.get("final_mode") != "patch_segment"
                if "runtime.completed" in event or "chat.error" in event or final_segment:
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
            "is_processing": any(
                record.has_pending_execution or record.is_processing
                for record in records if not record.is_passive_subscription
            ),
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
