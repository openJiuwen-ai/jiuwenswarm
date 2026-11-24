# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Delivery ledger and placeholder card (P6 silent-swallow backstop).

The supervisor registers a ledger entry at dispatch-accept time (deadline
wall clock); any delivered frame resets it (P7 liveness semantics: the
frame stream advancing means the worker is not stuck; whitelisted visible
frames additionally record ``last_visible_output_ts``).  When an entry
falls due without a flush, a ``chat.error`` placeholder card is sent
directly over the gateway socket following the placeholder-frame wire
five-element contract (construction precedent ``build_server_push_wire``
in ``server/gateway_push/wire.py``).  Control-plane requests
(``__cron__``/``__health_check__`` channels, ``interrupt_`` prefix)
legitimately produce zero visible replies and are skipped at registration.
The ledger stores delivery identity and cursors only — never user content.

Steering-input and interrupt-cancel misfire guards (#6638): a steering
message is registered under its own request_id while its visible reply is
merged into the parent turn and delivered under the parent request_id, so
its own entry never receives any frame; a cancelled/interrupted turn gets
no further frames either. Two guards close both paths: (1) session
liveness evidence -- once any *other* request of the same session has a
delivered frame after this entry was registered (P7 liveness semantics
extended to the session), the due entry is settled as ``retracted``
without sending a card; a session fully silent after registration (real
swallow, #6223) keeps firing the card as before. (2) cancel retracts the
card -- the CHAT_CANCEL registration exemption also retracts the
interrupted request (``target_request_id``, or all non-terminal entries of
the session for session-level cancel/supplement intents) to ``retracted``.

Full contract text:
``.agents/agent-docs/specs/jiuwenswarm/server/supervisor/S_01_delivery-ledger.md``.
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

# Top-level request_id suffix on the placeholder-card wire (a naturally
# distinct key from the real reply; the gateway opens its own stream per key).
PLACEHOLDER_REQUEST_SUFFIX = ":p6"

# Placeholder state machine: pending → pending_flush → sent (absorbing
# terminal); pending/pending_flush → retracted (undelivered placeholder
# cancelled, no wire action) / failed.
PLACEHOLDER_PENDING = "pending"
PLACEHOLDER_PENDING_FLUSH = "pending_flush"
PLACEHOLDER_SENT = "sent"
PLACEHOLDER_RETRACTED = "retracted"
PLACEHOLDER_FAILED = "failed"
PLACEHOLDER_TERMINAL_STATES = frozenset({PLACEHOLDER_SENT, PLACEHOLDER_RETRACTED, PLACEHOLDER_FAILED})

# User-visible whitelist: a delivery resets the deadline; in streaming
# mode chat.delta counts too.
VISIBLE_EVENT_WHITELIST = frozenset({"chat.final", "chat.error", "chat.ask_user_question"})
STREAM_VISIBLE_EVENT_WHITELIST = VISIBLE_EVENT_WHITELIST | {"chat.delta"}

# Control-plane exemption (root fix for P7 false positives): these
# requests produce zero user-visible frames by design (cron scheduling /
# health probes have no user channel to answer on; interrupt control
# operations carry no reply semantics), so registering them would only
# fire placeholder cards forever — skipped at registration time.
CONTROL_CHANNEL_IDS = frozenset({"__cron__", "__health_check__"})
CONTROL_REQUEST_ID_PREFIXES = ("interrupt_", "interrupt-",
                               # Intermediate control requests dispatched by cron (command
                               # acks / internal fetches): the worker never returns frames,
                               # so registering them only misfires a placeholder card at
                               # deadline (GMK-verified 2026-11-11 AM, twice: cron-command-
                               # ack-*/fetch-* entries state=sent seq=0, channel=web escaped
                               # the __cron__ channel exemption).
                               "cron-command-ack-", "fetch-")

# Delivery identity keys (mirror of the gateway's
# message_handler._DELIVERY_IDENTITY_METADATA_KEYS; unit tests assert both
# sides to prevent drift).  The ledger may store only these keys and never
# user content.
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

# Tiered persistence: batch-write thresholds for the sequence cursor and
# last_visible_output_ts (50 frames / 2s).
_BATCH_FRAMES = 50
_BATCH_SECONDS = 2.0

# Terminal reclamation: retention window after an entry enters a terminal
# state (sweep periodically reclaims memory and on-disk copies, with a log
# trail).
_REAP_SECONDS = 3600.0

_PLACEHOLDER_ERROR_TEXT = "长时间未收到回复，本次处理可能已中断"
_PLACEHOLDER_RESEND_HINT = "请重新发送该消息重试；如持续无响应，请稍后再试或联系管理员"

# Placeholder card reason families: the card must tell the user *what* happened
# and *what to do*, not just "maybe interrupted". Same-root family as #6638.
PLACEHOLDER_REASON_PIPELINE_SILENT = "pipeline_silent"
PLACEHOLDER_REASON_RESTART_INTERRUPTED = "restart_interrupted"
PLACEHOLDER_REASON_PARTIAL_THEN_SILENT = "partial_then_silent"

_PLACEHOLDER_TEXTS = {
    PLACEHOLDER_REASON_PIPELINE_SILENT: (
        _PLACEHOLDER_ERROR_TEXT,
        _PLACEHOLDER_RESEND_HINT,
    ),
    PLACEHOLDER_REASON_RESTART_INTERRUPTED: (
        "服务在本轮处理期间重启，这条消息还没有开始执行",
        "请重新发送该消息；服务重启不会自动重试在途或排队中的请求",
    ),
    PLACEHOLDER_REASON_PARTIAL_THEN_SILENT: (
        "已输出部分内容后中断，本次回复可能不完整",
        "请重新发送该消息以获取完整回复；如重复出现请联系管理员",
    ),
}


@dataclass
class LedgerEntry:
    scope: list[str] = field(default_factory=list)  # request_scope five-tuple (incl. session)
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
    """Request delivery ledger: wall-clock deadline + tiered persistence + placeholder state machine."""

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
        # Process boot wall clock: entries registered before it were loaded from
        # disk after a Front restart and can never be executed by this process.
        self._boot_ts = time.time()
        # Session liveness evidence (#6638): session_id -> {rid: last delivered
        # frame ts}. Keyed per rid on purpose (atomgit 2026-11-18 #10206): with
        # one last-writer-wins slot this entry's own cursor frames
        # (mark_sequence_only) overwrote sibling evidence and masked the
        # sibling liveness due_entries must see. In-memory derived surface
        # only, never persisted -- after a Front restart there is no evidence
        # and due entries fall back to the old fire-the-card behaviour
        # (fail-closed: prefer one false card over missing a real #6223
        # swallow).
        self._session_frame_ts: dict[str, dict[str, float]] = {}

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
        """Register at dispatch-accept time (synchronous write).

        Control-plane requests are skipped (with zero visible replies a
        placeholder card is meaningless).
        """
        from jiuwenswarm.common.schema.message import ReqMethod

        if getattr(request, "req_method", None) == ReqMethod.CHAT_CANCEL:
            # Cancel/interrupt exemption (AIReview SF-4): a cancel has zero
            # visible replies (chat.interrupt_result is not on the visible
            # whitelist) and process_cli cancels reuse the original rid —
            # registering would only misfire a placeholder card 600s after the
            # cancel plus reset the cursor.
            self._retract_interrupted(request)
            return None
        params = getattr(request, "params", None) or {}
        if params.get("attach_goal") is True or (
            getattr(request, "req_method", None) == ReqMethod.COMMAND_GOAL
            and str(params.get("action", "get")).strip().lower() not in {"set", "resume"}
        ):
            # Page restore (attach_goal) and passive Goal subscriptions
            # produce zero user-visible frames by design — criteria align with
            # the worker-side _is_session_work_request (atomgit id=200347100):
            # such web-channel requests would inevitably fall due after 600s
            # and misfire the "processing may have been interrupted"
            # placeholder card.  Active Goals (set/resume) have visible replies
            # and are registered as usual.
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
        # Team organization turns carry their target team (AIReview SF-4): a
        # targeted expert cancel must retract exactly this team's entries and
        # never the owner turn or sibling teams.
        target_team = str(params.get("target_team_id") or "").strip()
        if target_team and self._is_team_mode(params.get("mode")):
            identity["target_team_id"] = target_team
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
            # Same-rid re-registration: keep the max cursor — when a
            # process_cli cancel reuses the original rid and overwrites the
            # entry, resetting the cursor would break the after=sequence replay
            # contract (AIReview SF-4-③).
            entry.sequence = max(entry.sequence, prev_entry.sequence)
        self._entries[entry.request_id] = entry
        self._flush(entry)
        return entry

    def _retract_interrupted(self, request: AgentRequest) -> None:
        """Cancel retracts the interrupted turn's card (#6638 root cause 2).

        A cancelled/interrupted turn receives no further frames, so its
        pending/pending_flush entry is settled through the existing
        ``retracted`` channel (bookkeeping only, no wire action). Routing
        mirrors the CHAT_CANCEL dispatch in ``agent_ws_server``:

        * ``target_request_id`` -> targeted cancel: retract that entry.
        * ``organization_stop`` (full organization stop: every team turn
          dies) -> retract every non-terminal entry of the session.
        * ``organization_owner_stop`` -> owner-only semantics
          (``_cancel_organization`` owner_only branch): the expert team
          keeps running, its entries must keep their card guards -> no
          session-wide retract.
        * team mode with a ``target_team_id`` that differs from the session's
          durable owner team -> targeted expert cancel
          (``_cancel_targeted_expert``): only the target team's own turns
          die ("never the owner turn"), so retract exactly the entries
          registered under that team; the owner turn and sibling teams keep
          their card guards (AIReview SF-4). The runtime success gate
          requires ``intent == "cancel"`` — a refused targeted cancel
          (supplement intent / unknown owner team) interrupts nothing ->
          nothing is retracted.

        * Known gap (AIReview SF-5, #10208 sharing_posture precedent): the
          runtime success gate additionally requires ``same_organization``;
          when organization resolution fails at cancel time (team backend
          restart with rebuilt org, ``_load_persisted_organization``
          recovery error), the runtime refuses the targeted cancel while the
          ledger — which cannot observe runtime organization state — has
          already retracted the target team's entries. Narrow trigger
          (valid registration + org resolution failure at cancel), no wire
          action, no data loss: only the #6223 placeholder-card guard is
          disarmed for the affected entries (silent-failure visibility).
          ``test_m15_targeted_cancel_org_gate_gap_documented`` pins this
          documented behaviour.
        * otherwise ``intent`` (default ``"cancel"``, aligned with
          ``params.get("intent", "cancel")`` at the dispatch sites):
          cancel/supplement cancels all stream tasks of the session ->
          retract them; pause/resume keeps the turn running -> no retract.
        """
        params = getattr(request, "params", None) or {}
        target = str(params.get("target_request_id") or "").strip()
        if target:
            self.transition(target, PLACEHOLDER_RETRACTED, audit="chat_cancel_targeted")
            return
        if params.get("organization_owner_stop") is True:
            # owner-only stop: the expert team keeps running; a session-wide
            # retract would disarm the #6223 guard for their live entries.
            return
        intent = str(params.get("intent") or "cancel").strip().lower()
        session_id = str(request.session_id or "")
        if params.get("organization_stop") is True:
            # Full organization stop wins over the team-target shape
            # (agent_ws_server._handle_cancel routes it first): every team
            # turn dies -> session-wide retract.
            self._retract_session_entries(session_id,
                                          audit="chat_cancel_session_organization_stop")
            return
        target_team = str(params.get("target_team_id") or "").strip()
        if target_team and self._is_team_mode(params.get("mode")):
            owner_team = self._session_owner_team(session_id)
            if not owner_team:
                # Owner team unknown: the runtime refuses the targeted
                # cancel ("refused to fall back to a session-level cancel")
                # and cancels nothing -> retract nothing, keep all guards.
                return
            if target_team != owner_team:
                if intent != "cancel":
                    # Runtime success gate requires intent == "cancel".
                    return
                self._retract_team_entries(session_id, target_team)
                return
            # target_team == owner_team routes to the session-level cancel
            # (not _cancel_targeted_expert) -> fall through to session-wide.
        if intent not in {"cancel", "supplement"}:
            return
        if not session_id:
            return
        self._retract_session_entries(session_id,
                                      audit=f"chat_cancel_session_intent_{intent}")

    def _retract_session_entries(self, session_id: str, *, audit: str) -> int:
        """Retract every non-terminal entry of ``session_id`` (bookkeeping)."""
        count = 0
        for entry in list(self._entries.values()):
            if (entry.session_id == session_id
                    and entry.placeholder_state not in PLACEHOLDER_TERMINAL_STATES):
                self.transition(entry.request_id, PLACEHOLDER_RETRACTED, audit=audit)
                count += 1
        return count

    def _retract_team_entries(self, session_id: str, target_team: str) -> int:
        """Retract only entries registered under ``target_team`` in the session.

        Entry-side tagging happens at ``register`` (``identity_metadata
        ["target_team_id"]``), mirroring the expert chat.send routing shape.
        """
        count = 0
        for entry in list(self._entries.values()):
            if (entry.session_id == session_id
                    and str(entry.identity_metadata.get("target_team_id") or "") == target_team
                    and entry.placeholder_state not in PLACEHOLDER_TERMINAL_STATES):
                self.transition(entry.request_id, PLACEHOLDER_RETRACTED,
                                audit="chat_cancel_targeted_expert")
                count += 1
        return count

    @staticmethod
    def _is_team_mode(mode: Any) -> bool:
        """Mirror ``agent_gateway.common.mode_matrix.is_team_mode`` (lazy import)."""
        try:
            from agent_gateway.common.mode_matrix import is_team_mode

            return bool(is_team_mode(mode))
        except Exception:
            return False

    @staticmethod
    def _session_owner_team(session_id: str) -> str:
        """Durable owner team of the session (``team_name``), read-only.

        Mirrors ``agent_ws_server._team_target_and_owner``; the writeback is
        disabled so the cancel path never triggers the metadata inference
        side effect.
        """
        if not str(session_id or "").strip():
            return ""
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            metadata = get_session_metadata(session_id, enable_writeback=False) or {}
        except Exception:
            return ""
        if not isinstance(metadata, dict):
            return ""
        return str(metadata.get("team_name") or "").strip()

    def placeholder_reason(self, entry: LedgerEntry) -> str:
        """Classify why a due entry is firing a placeholder card.

        The user-facing card must state what actually happened and what to do
        (same-root family as #6638); a bare "maybe interrupted" text leaves the
        user unable to tell a system fault from their own message getting lost.

        * ``restart_interrupted``: registered before this process booted and
          never produced a frame -- a Front restart dropped it while queued or
          in flight; it will not be retried automatically.
        * ``partial_then_silent``: some frames were delivered, then the
          pipeline went quiet -- the reply may be truncated.
        * ``pipeline_silent``: nothing at all after registration (the #6223
          silent-swallow shape).
        """
        if entry.registered_at > 0 and entry.registered_at < self._boot_ts:
            if entry.sequence == 0 and entry.last_visible_output_ts == 0.0:
                return PLACEHOLDER_REASON_RESTART_INTERRUPTED
        if entry.sequence > 0 or entry.last_visible_output_ts > 0:
            return PLACEHOLDER_REASON_PARTIAL_THEN_SILENT
        return PLACEHOLDER_REASON_PIPELINE_SILENT

    def _note_session_frame(self, entry: LedgerEntry, now: float) -> None:
        """Record session-level liveness evidence (#6638): any delivered
        frame, including late frames of terminal entries."""
        if not entry.session_id:
            return
        frames = self._session_frame_ts.setdefault(entry.session_id, {})
        prev = frames.get(entry.request_id)
        if prev is None or now > prev:
            frames[entry.request_id] = now
        if len(frames) > 64:  # bounded: keep the freshest rids per session
            for stale_rid in sorted(frames, key=lambda r: frames[r])[: len(frames) - 64]:
                del frames[stale_rid]

    def note_frame_progress(self, request_id: str, now: float | None = None) -> None:
        """Liveness feed for frames that will never reach the wire (GitCode #6936).

        The external output gate drops gated-channel intermediate frames
        (tool/reasoning/delta) before forwarding, so ``on_wire_delivered``
        never sees them and a healthy long turn looks fully silent to the
        watchdog (GMK 2026-11-21: six misfires on 6k~18k-chunk turns; the
        P7 contract above assumes every advancing frame reaches the
        delivery path, which the gate breaks). This feed restores that
        contract for gated channels: reset the request's own deadline and
        record per-rid session liveness evidence (so a due supplemental-input
        entry retracts via the #6638 sibling guard). The frames were not
        user-visible: ``last_visible_output_ts`` and the placeholder state
        machine are deliberately untouched.
        """
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return
        now = time.time() if now is None else now
        self._note_session_frame(entry, now)
        if entry.placeholder_state in PLACEHOLDER_TERMINAL_STATES:
            return
        entry.deadline = now + self._idle_seconds
        entry.dirty_deadline = True
        entry.dirty_at = entry.dirty_at or now
        self._flush_batched(entry, now)

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
        """Any delivered frame resets the deadline (synchronous; liveness
        semantics in the module header); pending/pending_flush → retracted.

        sent is an absorbing terminal: once the placeholder card has been
        delivered, a late real reply only updates bookkeeping — no deadline
        reset and no state change (mutex semantics = no suppression; the late
        real reply is still delivered as usual).
        """
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return
        now = time.time() if now is None else now
        self._note_session_frame(entry, now)
        sequence = _wire_sequence(payload)
        if sequence > entry.sequence:
            entry.sequence = sequence
            entry.dirty_frames += 1
            entry.dirty_at = entry.dirty_at or now
        event = wire_event(payload)
        if event is None or event not in STREAM_VISIBLE_EVENT_WHITELIST:
            # Non-whitelist frames are liveness signals too (P7): a long
            # tool-only turn with zero visible frames is a healthy shape — the
            # frame stream advancing proves the worker is not stuck; the
            # deadline resets on any delivered frame, so the placeholder card
            # only alerts on a fully silent pipeline.  Persistence still goes
            # through the batched tier (this chain runs on the supervisor
            # event-loop thread; per-frame fsync is forbidden).
            #
            # Explicit trade-off (atomgit 199642764): worker-side keepalive
            # frames (agent_ws_server._activity_event) also land in this
            # branch and reset the deadline, so "visible silence" (no
            # whitelist frames in last_visible_output_ts for a long time) does
            # not participate in the placeholder-card decision — this PR's
            # alert semantics converge to "pipeline fully silent (frame stream
            # stopped)"; long tool turns with zero visible output are treated
            # as healthy.  last_visible_output_ts is kept for bookkeeping only,
            # reserving a data surface for a future dedicated visible-silence
            # watchdog.
            entry.deadline = now + self._idle_seconds
            entry.dirty_deadline = True
            entry.dirty_at = entry.dirty_at or now
            self._flush_batched(entry, now)
            return
        entry.last_visible_output_ts = now
        if entry.placeholder_state in PLACEHOLDER_TERMINAL_STATES:
            # Terminal entries (retracted/sent): late frames only update the
            # in-memory bookkeeping; deadline/cursor are flushed in batches
            # (tiered thresholds) — per-frame fsync is forbidden because this
            # chain runs on the supervisor event-loop thread and fsync per
            # frame would block IPC traffic on the same loop.
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
                # Only terminal frames (final/error/question) cancel the
                # placeholder-card backstop; chat.delta only resets the
                # deadline — on a streaming interruption (worker hung without
                # crashing) the entry stays pending, and the sweep sends the
                # placeholder card at expiry, keeping an error backstop for
                # the user.
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
            # Terminal absorption: terminal states cannot be transitioned out
            # of (prevents SENT overwriting RETRACTED under a sweep snapshot race).
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
        """Record consecutive send failures; move to failed at the threshold.

        Returns whether a terminal state was entered.
        """
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
        """Non-whitelist frame transferred successfully: advance the cursor only (batched), no deadline reset."""
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return
        self._note_session_frame(entry, time.time() if now is None else now)
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
        """Snapshot of all live entries (memory only)."""
        return list(self._entries.values())

    def due_entries(self, now: float | None = None) -> list[LedgerEntry]:
        """Due entries; #6638 sibling-liveness evidence settles them retracted."""
        now = time.time() if now is None else now
        due: list[LedgerEntry] = []
        for item in self._entries.values():
            if (item.placeholder_state not in PLACEHOLDER_TERMINAL_STATES
                    and item.deadline > 0 and item.deadline <= now):
                evidence = self._session_frame_ts.get(item.session_id)
                # A steered/interleaved request may itself receive some
                # non-visible frames and then go quiet while its visible
                # reply keeps flowing on the parent turn (2026-11-15 22:29
                # misfire: seq>0 entry fired a card while the parent rid was
                # still streaming at 22:46). Liveness evidence is therefore
                # "a *sibling* frame arrived after this entry's own last
                # frame" -- the entry's last activity equals
                # (deadline - idle_seconds).
                own_last_frame = item.deadline - self._idle_seconds
                live_evidence = bool(evidence) and item.registered_at > 0
                sibling_newer = False
                if live_evidence:
                    # Per-rid evidence (atomgit 2026-11-18 #10206): *any*
                    # sibling-rid frame after this entry's last frame counts;
                    # the entry's own frames still never self-prove (M3).
                    sibling_newer = any(
                        rid != item.request_id
                        and ts >= item.registered_at
                        and ts > own_last_frame
                        for rid, ts in evidence.items()
                    )
                if sibling_newer:
                    # Steering merged-reply blind spot (#6638 root cause 1):
                    # after this entry went quiet, some *other* request of the
                    # same session kept delivering frames -- the reply pipeline
                    # is progressing (P7 "frames advancing = worker alive"
                    # extended to session scope) and this rid's visible reply
                    # was merged into that stream. The card would be a false
                    # positive: settle as retracted (bookkeeping only, no wire
                    # action). When the whole session goes quiet together (no
                    # sibling frame after this entry's last frame) it is a real
                    # #6223 swallow and the card fires unchanged.
                    self.transition(item.request_id, PLACEHOLDER_RETRACTED,
                                    audit="session_liveness")
                    continue
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
        """Reclaim terminal entries: sent/retracted/failed past the retention window are deleted from memory and disk.

        Terminal time is taken from terminal_at (legacy on-disk records fall
        back to registered_at); a log line is emitted before each reclamation
        (request_id / terminal state / terminal age) to keep an audit trail on
        the log surface.  Returns the number of reclaimed entries.
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
        live_sessions = {item.session_id for item in self._entries.values()}
        for stale_session in [s for s in self._session_frame_ts if s not in live_sessions]:
            del self._session_frame_ts[stale_session]
        return reaped

    # ----------------------------------------------------------- persistence
    def load(self) -> int:
        """Front startup reload: read entries from disk one by one, judged
        by wall clock (deadline kept as-is; sweep decides expiry).
        """
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
        """Synchronous write (registration / deadline / state transition)."""
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
        """Batched cursor/visible-timestamp persistence: flush at the
        50-frame/2s thresholds; deadline-only changes also enter the window.
        """
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
    kind = payload.get("response_kind") if isinstance(payload.get("response_kind"), str) else None
    if kind == "e2a.error":
        # Terminal E2A error wires carry error semantics in response_kind only;
        # the body holds {code, message, details} without an event_type. The
        # gateway reconstructs event_type=chat.error at parse time
        # (e2a.wire_codec._mark_terminal_error_payload) — mirror that here so a
        # delivered error terminal retracts the ledger entry instead of firing
        # the placeholder card 600s after the user already saw the real error
        # (#6861: deadline was reset once by the error frame, entry stayed
        # pending, card misfired).
        return "chat.error"
    return kind


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


def build_placeholder_wire(entry: LedgerEntry, *, reason: str | None = None) -> dict[str, Any]:
    """Build the wire per the placeholder-frame five-element contract
    (precedent: the chunk branch of build_server_push_wire).

    1. top-level metadata carries E2A_WIRE_SERVER_PUSH_KEY (the gateway
       push-branch acceptance surface);
    2. top-level request_id=rid+":p6" (a top-level wire field; the gateway
       takes Message.id from this value);
    3. top-level session_id/channel_id;
    4. payload=chat.error (includes the original rid and the resend-hint text);
    5. metadata carries the request.metadata delivery identity keys
       (DELIVERY_IDENTITY_METADATA_KEYS) and app_id.
    """
    push_rid = placeholder_request_id(entry.request_id)
    reason = reason or PLACEHOLDER_REASON_PIPELINE_SILENT
    error_text, resend_hint = _PLACEHOLDER_TEXTS.get(
        reason, _PLACEHOLDER_TEXTS[PLACEHOLDER_REASON_PIPELINE_SILENT])
    payload = {
        "event_type": "chat.error",
        "error": error_text,
        "request_id": entry.request_id,
        "resend_hint": resend_hint,
        "reason": reason,
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


