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

from agent_gateway.common.session_message import SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY
from e2a.constants import E2A_WIRE_SERVER_PUSH_KEY

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
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
    # Set only by events carrying an explicit is_processing payload; distinguishes
    # the stream's own processing claim from busy flags set by other bookkeeping.
    stream_processing_claim: bool = False
    entered_at_ns: int | None = None
    interactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminal: dict[str, Any] | None = None
    delivery_generation: int | None = None
    completion: dict[str, Any] | None = None
    expired_sequence: int = 0

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
        # Per-request eviction watermark (replay_gapped): the global
        # _expired_sequence advances on ANY request's eviction, so keying the
        # gap check on it would turn any eviction into a false gap for every
        # other request. Keyed by (scope, request_id).
        self._expired_by_request: "OrderedDict[tuple, int]" = OrderedDict()
        # 水位上限裁剪的丢失账（atomgit 9722 id=200378287）：被 LRU 裁掉的键若
        # 静默缺位，replay_gapped 会把「水位曾存在」的请求误报无缺口→静默跳帧。
        # 被裁键在此留痕，replay_gapped 对留痕键保守报缺（history_required 回落
        # 占位卡安全面）。同上限 LRU，残余面需双重超限且请求闲置两段窗口。
        self._watermark_lost: "OrderedDict[tuple, None]" = OrderedDict()
        self._max_watermark_keys = 1024
        self.records: OrderedDict[str, DispatchRecord] = OrderedDict()
        self._active: dict[tuple[str, ...], str] = {}

    def register(self, request: AgentRequest, dispatch_id: str) -> DispatchRecord:
        scope = request_scope(request)
        # Active IDs belong to the caller; changing Session cannot bypass the
        # duplicate guard. Journal lookup and replay remain Session-scoped.
        key = (*scope[:4], str(request.request_id or ""))
        if key in self._active:
            raise ValueError("duplicate active request_id")
        # Returned handlers can still own pending interactions and keep their
        # duplicate guard, but they no longer need a dispatch slot.
        if sum(record.active for record in self.records.values()) >= 256:
            raise ValueError("worker dispatch limit reached")
        if len(self.records) >= 2048:
            raise ValueError("worker retained interaction limit reached")
        # 同 (scope, request_id) 重新登记＝新一轮轮次：旧轮淘汰水位必须复位，
        # 否则新轮 after=0 撞旧水位被误报缺口、身份型重放退化占位卡
        # （atomgit 9722 id=199422840）。
        self._expired_by_request.pop((scope, str(request.request_id or "")), None)
        self._watermark_lost.pop((scope, str(request.request_id or "")), None)
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
            self.records.pop(removable)
            # 重放缺口水位（_expired_by_request）不随记录修剪清理：水位生命周期
            # 跟随 _frames（帧淘汰时写入），记录重建后 replay_gapped 仍需水位
            # 判定缺口；有界性由写入侧容量上限保证（见 append 帧淘汰段）。

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
        record.stream_processing_claim = False
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
            rid = str(expired.get("request_id") or "")
            if rid:
                key = (_expired_scope, rid)
                if expired["sequence"] > self._expired_by_request.get(key, 0):
                    self._expired_by_request[key] = expired["sequence"]
                self._expired_by_request.move_to_end(key)
                # 重新获得水位＝丢失面消除（atomgit 9722 id=200378287）。
                self._watermark_lost.pop(key, None)
            self._bytes -= expired_size
            expired_record = self.records.get(expired["dispatch_id"])
            if expired_record is not None:
                expired_record.expired_sequence = expired["sequence"]
        # 水位图容量上限（防无界增长；AIReview SF-1 关注点）——
        # 生命周期跟随帧淘汰（不随记录修剪提前清空，atomgit 判词 199763306）；
        # LRU 触碰保证活跃请求的水位不被误清（atomgit 9707 id=199769142-⑤），
        # 超限淘汰最久未触碰条目（只影响已整体淘汰出保留窗的请求）。
        while len(self._expired_by_request) > self._max_watermark_keys:
            lost_key, lost_watermark = self._expired_by_request.popitem(last=False)
            if lost_watermark > 0:
                # 裁剪留痕：缺位不再等于「无缺口」（atomgit 9722 id=200378287）。
                self._watermark_lost[lost_key] = None
                self._watermark_lost.move_to_end(lost_key)
                while len(self._watermark_lost) > self._max_watermark_keys:
                    self._watermark_lost.popitem(last=False)
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
                    # The stream's own claim survives ledger pruning in
                    # dispatcher._reconcile_execution_state; only the stream's
                    # false or a terminal event retires it.
                    record.stream_processing_claim = bool(body["is_processing"])
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
                    record.stream_processing_claim = False
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

    def retire_session_interactions(self, request: AgentRequest) -> None:
        """Mark a session's pending interactions retired after an interrupt.

        chat.interrupt (pause/cancel) ends the current round; an ask_user the
        round was holding can no longer be answered and the paused session must
        not stay "processing". Without this the next metadata query sees
        is_processing + a buffered ask_user frame and the forced after:0
        replay re-opens the popup.

        A cancel with params.target_request_id retires only that request's
        record: the same scope also carries the owner turn and other rounds
        whose pending interactions must stay answerable. An already-returned
        record still counts — its suspended ask_user is as unanswerable as a
        live one once its round is the cancel target. Retiring a non-active
        record also releases its ``_active`` slot here: ``finish`` kept the
        slot because the suspended interaction made the record look busy, and
        without the release every retired round leaks one slot until the
        dispatch limit rejects all later work.
        """
        scope = request_scope(request)
        # Legacy clients pass the cancel target as request_id — same fallback
        # as admission._dispatch_cancel and dispatcher._drop_queued_cancel.
        params = request.params or {}
        target = str(params.get("target_request_id") or params.get("request_id") or "")
        retired: list[str] = []
        for record in self.records.values():
            if record.scope != scope:
                continue
            if record.is_passive_subscription:
                continue
            if target and str(record.request.request_id or "") != target:
                continue
            record.interactions.clear()
            record.is_processing = False
            record.stream_processing_claim = False
            if not record.active:
                retired.append(record.dispatch_id)
        # finish() may trim self.records, so it runs after the iteration;
        # active records keep their slot for the cancel/complete path.
        for dispatch_id in retired:
            self.finish(dispatch_id)

    def replay(self, request: AgentRequest, *, after: int) -> list[dict[str, Any]]:
        # Admission is decided by the dispatcher's recoverable-control filter,
        # which reads the live execution state. A dispatch record's pending
        # interactions cannot tell a finished question from the continuation
        # the same round is currently waiting on, so filtering here would drop
        # the live one.
        scope = request_scope(request)
        return [copy.deepcopy(frame) for owner, frame, _ in self._frames
                if owner == scope and frame["sequence"] > after]

    def replay_by_request(
        self, scope: tuple[str, ...], request_id: str, *, after: int = 0
    ) -> list[dict[str, Any]]:
        """P6 身份型重放：按 (scope, request_id) 取单请求保留帧（游标之后）。"""
        rid = str(request_id or "")
        return [copy.deepcopy(frame) for owner, frame, _ in self._frames
                if owner == scope and frame["request_id"] == rid and frame["sequence"] > after]

    def replay_gapped(self, scope: tuple[str, ...], request_id: str, after: int) -> bool:
        """True 若该请求自身的保留帧已被淘汰至 after 之下（重放会静默跳帧）。

        以 (scope, request_id) 级水位判定，而非全店共享的 _expired_sequence：
        全局水位在任意请求淘汰时前移，用它判缺口会把别的请求的淘汰误报成
        本请求的缺口，让身份型重放退化为占位卡。
        """
        rid = str(request_id or "")
        key = (scope, rid)
        if key in self._expired_by_request:
            return after < self._expired_by_request[key]
        # 水位曾被记录但被上限裁掉：无法证明无缺口 → 保守报缺，重放回落
        # history_required/占位卡安全面，不静默跳帧（atomgit id=200378287）。
        return key in self._watermark_lost
