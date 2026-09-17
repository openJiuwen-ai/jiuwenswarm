# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Evolution approval state coordination for the gateway message handler."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.schema.message import Message, ReqMethod

logger = logging.getLogger(__name__)

SKILL_EVOLUTION_APPROVAL_SCHEMA = "openjiuwen.skill_evolution_approval.v1"
SKILL_EVOLUTION_APPROVAL_SOURCE = "skill_evolution_approval"


@dataclass
class EvolutionApprovalChunkDecision:
    should_publish_chunk: bool = True
    user_message: Message | None = None


@dataclass
class DeferredEvolutionApproval:
    request_id: str
    chunk_request_id: str
    channel_id: str
    payload: dict[str, Any]
    metadata: dict[str, Any] | None = None


@dataclass
class EvolutionApprovalFinishResult:
    queued_supplement: dict[str, Any] | None = None
    promoted_approval: DeferredEvolutionApproval | None = None


def is_evolution_approval_request_id(request_id: Any) -> bool:
    # Support skill evolution (skill_evolve_*) and team skill evolution (team_skill_evolve_*).
    # Note: skill creation (SkillCreateRail/TeamSkillCreateRail) uses ask_user + skill-creator
    # flow, not the approval-based routing.
    return isinstance(request_id, str) and (
        request_id.startswith("skill_evolve_")
        or request_id.startswith("team_skill_evolve_")
    )


def is_evolution_approval_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if is_evolution_approval_request_id(payload.get("request_id")):
        return True
    if payload.get("source") == "evolution_interrupt":
        return True
    if payload.get("source") == SKILL_EVOLUTION_APPROVAL_SOURCE:
        return True
    if payload.get("approval_schema") == SKILL_EVOLUTION_APPROVAL_SCHEMA:
        return True

    evolution_meta = payload.get("evolution_meta")
    return isinstance(evolution_meta, dict) and evolution_meta.get("event_kind") == "approval"


def is_interrupt_evolution_approval_answer_payload(payload: Any) -> bool:
    if not is_evolution_approval_payload(payload):
        return False
    if str(payload.get("request_id") or "").startswith("call_"):
        return True
    if payload.get("source") == "evolution_interrupt":
        return True
    evolution_meta = payload.get("evolution_meta")
    return (
        isinstance(evolution_meta, dict)
        and evolution_meta.get("approval_transport") == "interrupt"
    )


def ensure_regular_evolution_approval_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["source"] = SKILL_EVOLUTION_APPROVAL_SOURCE
    enriched.setdefault("approval_schema", SKILL_EVOLUTION_APPROVAL_SCHEMA)
    evolution_meta = enriched.get("evolution_meta")
    if not isinstance(evolution_meta, dict):
        evolution_meta = {}
    evolution_meta = dict(evolution_meta)
    evolution_meta.setdefault("event_kind", "approval")
    evolution_meta.setdefault("rail_kind", "regular")
    evolution_meta.setdefault("approval_kind", "evolve")
    enriched["evolution_meta"] = evolution_meta
    return enriched


class EvolutionApprovalCoordinator:
    """Owns per-session gateway state for evolution approval."""

    def __init__(self) -> None:
        self._pending_evolution_approval: dict[str, str] = {}
        self._hidden_auto_saved_regular_approvals: dict[str, set[str]] = {}
        self._deferred_evolution_approvals: dict[str, list[DeferredEvolutionApproval]] = {}
        self._queued_supplement_input: dict[str, dict[str, Any]] = {}
        self._session_evolution_in_progress: set[str] = set()

    def is_current_pending(self, session_id: str | None, request_id: Any) -> bool:
        return (
            isinstance(session_id, str)
            and isinstance(request_id, str)
            and self._pending_evolution_approval.get(session_id) == request_id
        )

    def pending_request_id(self, session_id: str | None) -> str | None:
        if not isinstance(session_id, str):
            return None
        return self._pending_evolution_approval.get(session_id)

    def deferred_request_ids(self, session_id: str | None) -> list[str]:
        if not isinstance(session_id, str):
            return []
        return [
            approval.request_id
            for approval in self._deferred_evolution_approvals.get(session_id, [])
        ]

    def mark_pending(self, session_id: str | None, request_id: Any) -> None:
        if not session_id:
            return
        self._pending_evolution_approval[session_id] = str(request_id)

    def mark_session_in_progress(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._session_evolution_in_progress.add(session_id)

    def clear_session_in_progress(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._session_evolution_in_progress.discard(session_id)

    def is_session_in_progress(self, session_id: str | None) -> bool:
        return isinstance(session_id, str) and session_id in self._session_evolution_in_progress

    def should_queue_supplement(self, session_id: str | None) -> bool:
        return (
            self.is_session_in_progress(session_id)
            or self.pending_request_id(session_id) is not None
        )

    def queue_supplement(
        self,
        session_id: str | None,
        new_input: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        if not session_id:
            return
        payload: dict[str, Any] = {"new_input": new_input}
        if attachments:
            payload["attachments"] = attachments
        self._queued_supplement_input[session_id] = payload

    def queued_supplement(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        return self._queued_supplement_input.get(session_id)

    def pop_queued_supplement(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        return self._queued_supplement_input.pop(session_id, None)

    def clear_pending(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._pending_evolution_approval.pop(session_id, None)

    def finish_if_current(
        self,
        session_id: str | None,
        answered_request_id: str | None,
    ) -> EvolutionApprovalFinishResult | None:
        if not session_id or not answered_request_id:
            return None

        current_request_id = self._pending_evolution_approval.get(session_id)
        if current_request_id != answered_request_id:
            hidden = self._hidden_auto_saved_regular_approvals.get(session_id)
            if hidden and answered_request_id in hidden:
                hidden.discard(answered_request_id)
                if not hidden:
                    self._hidden_auto_saved_regular_approvals.pop(session_id, None)
                if current_request_id is None:
                    self.clear_session_in_progress(session_id)
                logger.info(
                    "[MessageHandler] hidden regular evolution approval resolved: "
                    "session_id=%s answered=%s current=%s",
                    session_id,
                    answered_request_id,
                    current_request_id,
                )
                return EvolutionApprovalFinishResult()
            logger.info(
                "[MessageHandler] stale evolution approval resolved, "
                "keep current pending: session_id=%s answered=%s current=%s",
                session_id,
                answered_request_id,
                current_request_id,
            )
            return None

        self._pending_evolution_approval.pop(session_id, None)
        self.clear_session_in_progress(session_id)
        promoted = self._promote_deferred(session_id)
        if promoted is not None:
            return EvolutionApprovalFinishResult(promoted_approval=promoted)
        return EvolutionApprovalFinishResult(
            queued_supplement=self._queued_supplement_input.pop(session_id, None)
        )

    def clear_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._session_evolution_in_progress.discard(session_id)
        self._pending_evolution_approval.pop(session_id, None)
        self._hidden_auto_saved_regular_approvals.pop(session_id, None)
        self._deferred_evolution_approvals.pop(session_id, None)
        self._queued_supplement_input.pop(session_id, None)

    def clear_all(self) -> None:
        self._session_evolution_in_progress.clear()
        self._pending_evolution_approval.clear()
        self._hidden_auto_saved_regular_approvals.clear()
        self._deferred_evolution_approvals.clear()
        self._queued_supplement_input.clear()

    def handle_chunk(
        self,
        chunk: Any,
        session_id: str | None,
        request_metadata: dict[str, Any] | None = None,
        *,
        auto_save_enabled: bool,
    ) -> EvolutionApprovalChunkDecision:
        if not isinstance(chunk.payload, dict):
            return EvolutionApprovalChunkDecision()

        event_type = chunk.payload.get("event_type")
        if event_type == "chat.evolution_status":
            self._handle_evolution_status(chunk, session_id)

        approval_request_id = chunk.payload.get("request_id")
        if event_type != "chat.ask_user_question" or not is_evolution_approval_payload(chunk.payload):
            return EvolutionApprovalChunkDecision()

        incoming_request_id = str(approval_request_id)
        is_interrupt_approval = is_interrupt_evolution_approval_answer_payload(chunk.payload)
        decision = self._decide_approval_chunk(
            chunk=chunk,
            session_id=session_id,
            incoming_request_id=incoming_request_id,
            channel_id=str(getattr(chunk, "channel_id", "") or ""),
            metadata=request_metadata,
            auto_save_enabled=auto_save_enabled,
            is_interrupt_approval=is_interrupt_approval,
        )
        if not decision.should_publish_chunk:
            return decision

        self.mark_pending(session_id, approval_request_id)
        logger.info(
            "[MessageHandler] evolution approval detected: session_id=%s request_id=%s",
            session_id,
            approval_request_id,
        )
        return decision

    def _handle_evolution_status(self, chunk: Any, session_id: str | None) -> None:
        status = str(chunk.payload.get("status", "")).strip().lower()
        rid = getattr(chunk, "request_id", "")
        if status == "start":
            self.mark_session_in_progress(session_id)
            logger.info(
                "[MessageHandler] evolution status start: session_id=%s request_id=%s",
                session_id,
                rid,
            )
        elif status == "end":
            self.clear_session_in_progress(session_id)
            logger.info(
                "[MessageHandler] evolution status end: session_id=%s request_id=%s",
                session_id,
                rid,
            )

    def _decide_approval_chunk(
        self,
        *,
        chunk: Any,
        session_id: str | None,
        incoming_request_id: str,
        channel_id: str,
        metadata: dict[str, Any] | None,
        auto_save_enabled: bool,
        is_interrupt_approval: bool,
    ) -> EvolutionApprovalChunkDecision:
        if not session_id or not incoming_request_id:
            return EvolutionApprovalChunkDecision()

        if auto_save_enabled and not is_interrupt_approval:
            self._hidden_auto_saved_regular_approvals.setdefault(session_id, set()).add(
                incoming_request_id
            )
            auto_answer = self._build_auto_accept_answer(
                channel_id=channel_id,
                session_id=session_id,
                request_id=incoming_request_id,
                metadata=metadata,
            )
            logger.info(
                "[MessageHandler] auto-accept evolution approval: session_id=%s request_id=%s",
                session_id,
                incoming_request_id,
            )
            return EvolutionApprovalChunkDecision(
                should_publish_chunk=False,
                user_message=auto_answer,
            )

        previous_request_id = self._pending_evolution_approval.get(session_id)
        if previous_request_id and previous_request_id != incoming_request_id:
            self._defer(
                session_id=session_id,
                request_id=incoming_request_id,
                chunk=chunk,
                metadata=metadata,
            )
            return EvolutionApprovalChunkDecision(should_publish_chunk=False)

        return EvolutionApprovalChunkDecision()

    @staticmethod
    def _build_auto_accept_answer(
        *,
        channel_id: str,
        session_id: str,
        request_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        params = ensure_regular_evolution_approval_metadata({
            "request_id": request_id,
            "answers": [{"selected_options": ["接收"]}],
        })

        return Message(
            id=f"auto_evolve_answer_{int(time.time() * 1000):x}_{secrets.token_hex(3)}",
            type="req",
            channel_id=channel_id,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_ANSWER,
            is_stream=False,
            metadata=metadata,
        )

    def _defer(
        self,
        *,
        session_id: str,
        request_id: str,
        chunk: Any,
        metadata: dict[str, Any] | None,
    ) -> None:
        deferred = self._deferred_evolution_approvals.setdefault(session_id, [])
        if not any(approval.request_id == request_id for approval in deferred):
            deferred.append(
                DeferredEvolutionApproval(
                    request_id=request_id,
                    chunk_request_id=str(getattr(chunk, "request_id", "") or ""),
                    channel_id=str(getattr(chunk, "channel_id", "") or ""),
                    payload=dict(chunk.payload),
                    metadata=dict(metadata) if metadata is not None else None,
                )
            )
        logger.info(
            "[MessageHandler] defer superseding evolution approval until current resolves: "
            "session_id=%s current=%s deferred=%s",
            session_id,
            self._pending_evolution_approval.get(session_id),
            request_id,
        )

    def _promote_deferred(self, session_id: str) -> DeferredEvolutionApproval | None:
        deferred = self._deferred_evolution_approvals.get(session_id)
        if not deferred:
            self._deferred_evolution_approvals.pop(session_id, None)
            return None
        next_approval = deferred.pop(0)
        if not deferred:
            self._deferred_evolution_approvals.pop(session_id, None)
        self.mark_pending(session_id, next_approval.request_id)
        self.mark_session_in_progress(session_id)
        logger.info(
            "[MessageHandler] promoted deferred evolution approval: session_id=%s request_id=%s",
            session_id,
            next_approval.request_id,
        )
        return next_approval
