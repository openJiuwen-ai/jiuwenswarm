# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Delivery ledger and placeholder card (P6 silent-reply-swallowing backstop).

The supervisor registers the ledger entry at dispatch accept (wall-clock deadline); any delivered frame resets
it (P7 liveness semantics: advancing frame flow = worker alive; visible-whitelisted frames additionally record
``last_visible_output_ts``); on expiry without settlement the placeholder card is built per the five-element wire contract (precedent
``server/gateway_push/wire.py`` ``build_server_push_wire``) and delivered directly over the gateway socket as a chat.error placeholder.
Control-plane requests (``__cron__``/``__health_check__`` channels and the interrupt_ prefix) have zero visible replies by design, skipped at register time.
The ledger stores only delivery identity and cursors, never user content.
See the spec for the full contract.

The full contract lives in the spec S_01_delivery-ledger.md.
``.agents/agent-docs/specs/jiuwenswarm/server/supervisor/S_01_delivery-ledger.md``。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from e2a.wire_codec import encode_agent_chunk_for_wire
from e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk

logger = logging.getLogger(__name__)

# Placeholder top-level request_id suffix (a key distinct from real replies; the gateway builds an independent stream per key).
PLACEHOLDER_REQUEST_SUFFIX = ":p6"

# Placeholder state machine: pending -> pending_flush -> sent (absorbing terminal);
# pending/pending_flush -> retracted (cancel an undelivered placeholder, no wire action) / failed.
PLACEHOLDER_PENDING = "pending"
PLACEHOLDER_PENDING_FLUSH = "pending_flush"
PLACEHOLDER_SENT = "sent"
PLACEHOLDER_RETRACTED = "retracted"
PLACEHOLDER_FAILED = "failed"
PLACEHOLDER_TERMINAL_STATES = frozenset({PLACEHOLDER_SENT, PLACEHOLDER_RETRACTED, PLACEHOLDER_FAILED})

# User-visible whitelist: a hit resets the deadline; in streaming mode chat.delta also counts.
VISIBLE_EVENT_WHITELIST = frozenset({"chat.final", "chat.error", "chat.ask_user_question"})
STREAM_VISIBLE_EVENT_WHITELIST = VISIBLE_EVENT_WHITELIST | {"chat.delta"}

# Control-plane exemption (P7 misfire root fix): these requests have zero visible replies by design
# (cron scheduling / health probes have no user channel to reply to; interrupt control ops have no reply semantics),
# so registering them would always fire a card — skip them at register time.
CONTROL_CHANNEL_IDS = frozenset({"__cron__", "__health_check__"})
CONTROL_REQUEST_ID_PREFIXES = ("interrupt_", "interrupt-",
                               # Intermediate control requests dispatched by cron (command acks / internal fetches):
                               # the worker never replies with frames, so registering would always fire a card at expiry
                               # (two live occurrences on 2026-11-11 morning: cron-command-ack-*/fetch-*
                               #   entries state=sent seq=0, channel=web escaped the __cron__ channel exemption).
                               "cron-command-ack-", "fetch-")

# Delivery identity keys (mirror of the gateway message_handler._DELIVERY_IDENTITY_METADATA_KEYS;
# unit tests assert both sides equal to prevent drift). The ledger stores only the intersection of these keys, never user content.
DELIVERY_IDENTITY_METADATA_KEYS = frozenset({
    "app_id",
    "chat_type",
    "im_chat_type",
    "feishu_chat_id",
    "feishu_open_id",
    "open_id",
    "im_sender_user_id",
    "im_thread_id",
})

# Graded persistence: batched-write thresholds for the sequence cursor and last_visible_output_ts (50 frames / 2s).
_BATCH_FRAMES = 50
_BATCH_SECONDS = 2.0

# Terminal-entry reaping: retention window after an entry goes terminal (the sweep reclaims memory and disk copies, logging for audit).
_REAP_SECONDS = 3600.0

_PLACEHOLDER_ERROR_TEXT = "长时间未收到回复，本次处理可能已中断"
_PLACEHOLDER_RESEND_HINT = "请重新发送该消息重试；如持续无响应，请稍后再试或联系管理员"


@dataclass
class LedgerEntry:
    scope: list[str] = field(default_factory=list)  # request_scope five-tuple (includes session)
    channel_id: str = ""
    user_id: str = ""
    app_id: str = ""
    agent_ref_json: str = ""
    session_id: str = ""
    request_id: str = ""
    dispatch_id: str = ""
    worker_instance: str = ""
    sequence: int = 0
    last_visible_output_ts: float = 0.0
    deadline: float = 0.0
    placeholder_state: str = PLACEHOLDER_PENDING
    dump_ref: str = ""
    registered_at: float = 0.0
    send_failures: int = 0
    terminal_at: float = 0.0
    identity_metadata: dict[str, Any] = field(default_factory=dict)
    dirty_frames: int = 0
    dirty_at: float = 0.0
    dirty_deadline: bool = False


class DeliveryLedger:
    """Request delivery ledger: wall-clock deadline + graded persistence + placeholder state machine."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        idle_seconds: float = 600.0,
        expire_seconds: float = 24 * 3600.0,
        max_push_failures: int = 3,
        reap_seconds: float = _REAP_SECONDS,
    ) -> None:
        self._directory = Path(directory) if directory is not None else None
        self._idle_seconds = idle_seconds
        self._expire_seconds = expire_seconds
        self._reap_seconds = reap_seconds
        self._max_push_failures = max_push_failures
        self._entries: dict[str, LedgerEntry] = {}

    # ------------------------------------------------------------------ dir
    @property
    def directory(self) -> Path:
        if self._directory is None:
            from jiuwenswarm.common.utils import get_delivery_ledger_dir

            self._directory = get_delivery_ledger_dir()
        return self._directory

    # ------------------------------------------------------------ mutation
    def register(
        self,
        request: AgentRequest,
        *,
        dispatch_id: str,
        worker_instance: str,
        deadline: float,
        dump_ref: str = "",
    ) -> LedgerEntry | None:
        """Register at dispatch accept (sync write). Control-plane requests are skipped (zero visible replies make a placeholder card meaningless)."""
        from jiuwenswarm.common.schema.message import ReqMethod

        if getattr(request, "req_method", None) == ReqMethod.CHAT_CANCEL:
# Cancel/interrupt exemption (AIReview SF-4): a cancel has zero visible replies (chat.interrupt_result
# is not in the visible whitelist) and process_cli cancel reuses the original rid — registering would only cause
# a false card 600s later plus a cursor wipe.
            return None
        params = getattr(request, "params", None) or {}
        if params.get("attach_goal") is True or (
            getattr(request, "req_method", None) == ReqMethod.COMMAND_GOAL
            and str(params.get("action", "get")).strip().lower() not in {"set", "resume"}
        ):
# Page restore (attach_goal) and passive goal subscription have zero visible frames by design — the criteria align with
# the worker-side _is_session_work_request (atomgit id=200347100):
# these web-channel requests would always hit the 600s expiry with a false "process may have been interrupted"
# card. An active goal command (set/resume) has visible replies and registers normally.
            logger.info(
                "[DeliveryLedger] non-session-work request skipped request_id=%s",
                request.request_id,
            )
            return None
        metadata = request.metadata or {}
        if (str(getattr(request, "channel_id", "") or "") in CONTROL_CHANNEL_IDS
                or str(request.request_id or "").startswith(CONTROL_REQUEST_ID_PREFIXES)):
            logger.info(
                "[DeliveryLedger] control-plane request skipped request_id=%s channel_id=%s",
                request.request_id, getattr(request, "channel_id", ""),
            )
            return None
        identity = {
            key: metadata[key]
            for key in DELIVERY_IDENTITY_METADATA_KEYS
            if key in metadata and metadata[key] is not None
        }
        from jiuwenswarm.server.ipc.protocol import request_scope

        scope = list(request_scope(request))
        agent_ref_json = scope[3] if len(scope) > 3 else ""
        entry = LedgerEntry(
            scope=scope,
            channel_id=str(request.channel_id or ""),
            user_id=str(request.user_id or metadata.get("user_id") or ""),
            app_id=str(metadata.get("app_id") or ""),
            agent_ref_json=agent_ref_json,
            session_id=str(request.session_id or ""),
            request_id=str(request.request_id or ""),
            dispatch_id=str(dispatch_id or ""),
            worker_instance=str(worker_instance or ""),
            deadline=float(deadline),
            dump_ref=dump_ref,
            registered_at=time.time(),
            identity_metadata=identity,
        )
        prev_entry = self._entries.get(entry.request_id)
        if prev_entry is not None:
# Same-rid re-register: take the max cursor — when process_cli cancel reuses the original rid and overwrites the entry,
# a wiped cursor would break the after=sequence replay contract (AIReview SF-4-③).
            entry.sequence = max(entry.sequence, prev_entry.sequence)
        self._entries[entry.request_id] = entry
        self._flush(entry)
        return entry

    def pop(self, request_id: str) -> None:
        self._entries.pop(request_id, None)
        path = self._entry_path(request_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[DeliveryLedger] remove failed request_id=%s", request_id)

    def on_wire_delivered(
        self, *, request_id: str, payload: dict[str, Any], now: float | None = None
    ) -> None:
        """Any delivered frame: reset the deadline (synchronous; liveness semantics in the module header); pending/pending_flush turn retracted.

        sent is an absorbing terminal: a late real reply after the card was sent only updates bookkeeping, no deadline reset,
        no state change (mutual exclusion = no suppression; the late real reply is still delivered).
        """
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return
        now = time.time() if now is None else now
        sequence = _wire_sequence(payload)
        if sequence > entry.sequence:
            entry.sequence = sequence
            entry.dirty_frames += 1
            entry.dirty_at = entry.dirty_at or now
        event = wire_event(payload)
        if event is None or event not in STREAM_VISIBLE_EVENT_WHITELIST:
            # Non-whitelisted frames are also a liveness signal (P7): long tool-only rounds with zero visible frames are
            # healthy as long as the frame flow advances — the deadline resets on any delivered frame; the card only fires for
            # a fully silent pipeline. Persistence stays graded/batched (this chain runs on the supervisor event-loop thread,
            # per-frame fsync is forbidden).
            #
            # Explicit trade-off (atomgit 199642764): worker keepalive frames (agent_ws_server._activity_event) also land in
            # this branch and reset the deadline; therefore "visible silence" (no whitelisted frame for a long while) is NOT part of
            # the card judgment — this PR alarm semantics converge to "pipeline fully silent (frame flow stopped)"; zero visible
            # output during long tool rounds is treated as healthy. last_visible_output_ts is kept as bookkeeping for a future
            # standalone visible-silence watchdog.

            entry.deadline = now + self._idle_seconds
            entry.dirty_deadline = True
            entry.dirty_at = entry.dirty_at or now
            self._flush_batched(entry, now)
            return
        entry.last_visible_output_ts = now
        if entry.placeholder_state in PLACEHOLDER_TERMINAL_STATES:
            # Terminal entries (retracted/sent): late frames only update in-memory bookkeeping; deadline/cursor go through batched
            # persistence (graded thresholds) — no per-frame fsync; that chain runs on the supervisor event-loop thread and
            # per-frame fsync would block IPC traffic on the same loop.
            if entry.placeholder_state == PLACEHOLDER_RETRACTED:
                entry.deadline = now + self._idle_seconds
                entry.dirty_deadline = True
                entry.dirty_at = entry.dirty_at or now
            self._flush_batched(entry, now)
            return
        entry.deadline = now + self._idle_seconds
        entry.dirty_deadline = True
        entry.dirty_at = entry.dirty_at or now
        if entry.placeholder_state in {PLACEHOLDER_PENDING, PLACEHOLDER_PENDING_FLUSH}:
            if event in VISIBLE_EVENT_WHITELIST:
                # Only terminal frames (final/error/question) cancel the card backstop; chat.delta only resets the deadline —
                # a mid-stream interruption (worker stuck, not crashed) leaves the entry pending and the card still fires at expiry,
                # giving the user an error backstop.
                entry.placeholder_state = PLACEHOLDER_RETRACTED
                entry.terminal_at = now
                self._flush(entry)
                return
        self._flush_batched(entry, now)

    def transition(self, request_id: str, state: str, *, audit: str = "") -> bool:
        entry = self._entries.get(str(request_id or ""))
        if entry is None or entry.placeholder_state == state:
            return False
        if entry.placeholder_state in PLACEHOLDER_TERMINAL_STATES:
            # Terminal absorb: terminal states cannot migrate out (prevents SENT overwriting RETRACTED under a sweep snapshot race).
            logger.info(
                "[DeliveryLedger] transition rejected from terminal state=%s request_id=%s audit=%s",
                entry.placeholder_state, entry.request_id, audit,
            )
            return False
        entry.placeholder_state = state
        if state in PLACEHOLDER_TERMINAL_STATES:
            entry.terminal_at = time.time()
        self._flush(entry)
        if state == PLACEHOLDER_FAILED:
            logger.warning(
                "[DeliveryLedger] placeholder %s request_id=%s dispatch_id=%s %s",
                state, entry.request_id, entry.dispatch_id, audit,
            )
        return True

    def note_push_failure(self, request_id: str) -> bool:
        """Record consecutive send failures; converts to failed at the threshold. Returns whether it went terminal."""
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return False
        entry.send_failures += 1
        if entry.send_failures >= self._max_push_failures:
            self.transition(request_id, PLACEHOLDER_FAILED, audit="push_failures_exhausted")
            return True
        self._flush(entry)
        return False

    def mark_sequence_only(self, request_id: str, payload: dict[str, Any], now: float | None = None) -> None:
        """Non-whitelisted frame transported OK: advance the cursor only (batched), no deadline reset."""
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return
        sequence = _wire_sequence(payload)
        if sequence > entry.sequence:
            entry.sequence = sequence
            entry.dirty_frames += 1
            entry.dirty_at = entry.dirty_at or (time.time() if now is None else now)
            self._flush_batched(entry, time.time() if now is None else now)

    # ------------------------------------------------------------- queries
    def entry(self, request_id: str) -> LedgerEntry | None:
        return self._entries.get(str(request_id or ""))

    def entries(self) -> list[LedgerEntry]:
        return list(self._entries.values())

    def due_entries(self, now: float | None = None) -> list[LedgerEntry]:
        now = time.time() if now is None else now
        due: list[LedgerEntry] = []
        for item in self._entries.values():
            if (item.placeholder_state not in PLACEHOLDER_TERMINAL_STATES
                    and item.deadline > 0 and item.deadline <= now):
                due.append(item)
        return due

    def expired_entries(self, now: float | None = None) -> list[LedgerEntry]:
        now = time.time() if now is None else now
        expired: list[LedgerEntry] = []
        for item in self._entries.values():
            if (item.placeholder_state not in PLACEHOLDER_TERMINAL_STATES
                     and item.registered_at > 0 and now - item.registered_at > self._expire_seconds):
                expired.append(item)
        return expired

    def reap_terminal(self, now: float | None = None) -> int:
        """Reap terminal entries: sent/retracted/failed older than the retention window get their memory and disk copies deleted.

        Terminal time comes from terminal_at (older on-disk entries fall back to registered_at); log before reaping
        (request_id / terminal state / terminal age) to keep the audit trail in the log face. Returns the reaped count.
        """
        now = time.time() if now is None else now
        reaped = 0
        for entry in list(self._entries.values()):
            if entry.placeholder_state not in PLACEHOLDER_TERMINAL_STATES:
                continue
            settled_at = entry.terminal_at or entry.registered_at
            if not settled_at or now - settled_at <= self._reap_seconds:
                continue
            self._entries.pop(entry.request_id, None)
            try:
                self._entry_path(entry.request_id).unlink(missing_ok=True)
            except OSError:
                logger.warning("[DeliveryLedger] reap unlink failed request_id=%s",
                               entry.request_id, exc_info=True)
            logger.info(
                "[DeliveryLedger] reaped terminal state=%s age=%.0fs request_id=%s",
                entry.placeholder_state, now - settled_at, entry.request_id,
            )
            reaped += 1
        return reaped

    # ----------------------------------------------------------- persistence
    def load(self) -> int:
        """Front-startup reload: read entries one by one, judged by wall clock (deadline kept as-is; the sweep judges expiry)."""
        self._entries.clear()
        directory = self.directory
        if not directory.is_dir():
            return 0
        count = 0
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("[DeliveryLedger] unreadable ledger file %s", path.name)
                continue
            if not isinstance(data, dict) or not str(data.get("request_id") or ""):
                continue
            entry = LedgerEntry(**{key: value for key, value in data.items()
                                   if key in LedgerEntry.__dataclass_fields__})
            self._entries[entry.request_id] = entry
            count += 1
        return count

    def _entry_path(self, request_id: str) -> Path:
        return self.directory / f"{_safe_name(request_id)}.json"

    def _flush(self, entry: LedgerEntry) -> None:
        """Synchronous write (register / deadline / state transitions)."""
        entry.dirty_frames = 0
        entry.dirty_at = 0.0
        entry.dirty_deadline = False
        data = asdict(entry)
        for key in ("dirty_frames", "dirty_at", "dirty_deadline"):
            data.pop(key, None)
        payload = json.dumps(data, ensure_ascii=False, default=str)
        path = self._entry_path(entry.request_id)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            logger.warning("[DeliveryLedger] flush failed request_id=%s", entry.request_id, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _flush_batched(self, entry: LedgerEntry, now: float) -> None:
        """Batched flush for cursor / visible timestamps: 50 frames / 2s thresholds; deadline-only changes enter the window too."""
        if entry.dirty_frames == 0 and not entry.dirty_deadline:
            return
        if entry.dirty_frames >= _BATCH_FRAMES or now - (entry.dirty_at or now) >= _BATCH_SECONDS:
            self._flush(entry)


def _safe_name(request_id: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_." else "_" for char in str(request_id))
    return cleaned[:160] or "unknown"


def wire_event(payload: dict[str, Any]) -> str | None:
    body = payload.get("body") or payload.get("payload")
    if isinstance(body, dict):
        nested = body.get("delta") if isinstance(body.get("delta"), dict) else body.get("result")
        if isinstance(nested, dict):
            body = {**body, **nested}
        event = body.get("event_type")
        if isinstance(event, str) and event:
            return event
    return payload.get("response_kind") if isinstance(payload.get("response_kind"), str) else None


def _wire_sequence(payload: dict[str, Any]) -> int:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        runtime_event = metadata.get("runtime_event")
        if isinstance(runtime_event, dict):
            try:
                return int(runtime_event.get("sequence") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def placeholder_request_id(request_id: str) -> str:
    return f"{request_id}{PLACEHOLDER_REQUEST_SUFFIX}"


def build_placeholder_wire(entry: LedgerEntry) -> dict[str, Any]:
    """Build the wire per the five-element placeholder contract (build_server_push_wire chunk-branch precedent).

    1. Top-level metadata carries E2A_WIRE_SERVER_PUSH_KEY (the gateway push-branch acceptance face);
    2. Top-level request_id = rid + ":p6" (top-level wire field; the gateway Message.id takes this value);
    3. Top-level session_id / channel_id;
    4. payload = chat.error (with the original rid and resend-hint copy);
    5. metadata carries the request.metadata delivery-identity keys (DELIVERY_IDENTITY_METADATA_KEYS) and app_id.
    """
    push_rid = placeholder_request_id(entry.request_id)
    payload = {
        "event_type": "chat.error",
        "error": _PLACEHOLDER_ERROR_TEXT,
        "request_id": entry.request_id,
        "resend_hint": _PLACEHOLDER_RESEND_HINT,
    }
    chunk = AgentResponseChunk(
        request_id=push_rid,
        channel_id=entry.channel_id,
        payload=payload,
        is_complete=True,
        agent_ref=None,
    )
    wire = encode_agent_chunk_for_wire(chunk, response_id=push_rid, sequence=0, is_stream=False)
    metadata = dict(wire.get("metadata") or {})
    for key, value in (entry.identity_metadata or {}).items():
        metadata.setdefault(key, value)
    if entry.app_id:
        metadata.setdefault("app_id", entry.app_id)
    metadata[E2A_WIRE_SERVER_PUSH_KEY] = True
    wire["metadata"] = metadata
    wire["session_id"] = entry.session_id or wire.get("session_id") or ""
    wire["channel_id"] = entry.channel_id
    return wire


