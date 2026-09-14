# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight, process-local product Session facts for KVC lifecycle.

This module deliberately owns no provider/model objects and writes no files.
It only turns authoritative product events into deduplicated logical actions;
Session delegates execution and ordering to the shared KVC runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


KVCGuardAction = Literal["offload", "prefetch", "evict"]


@dataclass(frozen=True, slots=True)
class KVCGuardActionRequest:
    action: KVCGuardAction
    session_id: str
    channel_id: str
    is_team: bool


@dataclass(slots=True)
class SessionKVCFacts:
    session_id: str
    channel_id: str = "default"
    is_team: bool = False
    foreground_view_ids: set[str] = field(default_factory=set)
    running_tasks: int = 0
    has_completed_inference: bool = False
    has_history: bool = False
    last_prepare_intent_id: str | None = None
    deleted: bool = False


class SessionKVCacheTaskGuard:
    """Convert foreground/task facts into minimal logical KVC actions."""

    def __init__(self) -> None:
        self._facts: dict[str, SessionKVCFacts] = {}

    def clear(self) -> None:
        self._facts.clear()

    def forget(self, session_id: str) -> None:
        self._facts.pop(_normalize(session_id), None)

    def snapshot(self, session_id: str) -> SessionKVCFacts | None:
        return self._facts.get(_normalize(session_id))

    def set_foreground(
        self,
        *,
        session_id: str,
        view_id: str,
        visible: bool,
        channel_id: str,
        is_team: bool,
        has_history: bool = False,
    ) -> KVCGuardActionRequest | None:
        facts = self._ensure(
            session_id,
            channel_id=channel_id,
            is_team=is_team,
            has_history=has_history,
        )
        if facts is None or facts.deleted:
            return None
        normalized_view_id = _normalize(view_id) or "default-view"
        if visible:
            facts.foreground_view_ids.add(normalized_view_id)
            # Merely looking at a Session does not prefetch it.
            return None

        facts.foreground_view_ids.discard(normalized_view_id)
        return self._maybe_offload(facts)

    def prepare(
        self,
        *,
        session_id: str,
        intent_id: str,
        channel_id: str,
        is_team: bool,
        has_history: bool = False,
    ) -> KVCGuardActionRequest | None:
        facts = self._ensure(
            session_id,
            channel_id=channel_id,
            is_team=is_team,
            has_history=has_history,
        )
        if facts is None or facts.deleted:
            return None
        normalized_intent_id = _normalize(intent_id)
        if (
            normalized_intent_id
            and facts.last_prepare_intent_id == normalized_intent_id
        ):
            return None
        facts.last_prepare_intent_id = normalized_intent_id or None
        if not (facts.has_completed_inference or facts.has_history):
            return None
        return self._request(facts, "prefetch")

    def task_started(
        self,
        *,
        session_id: str,
        channel_id: str,
        is_team: bool,
        has_history: bool = False,
    ) -> KVCGuardActionRequest | None:
        facts = self._ensure(
            session_id,
            channel_id=channel_id,
            is_team=is_team,
            has_history=has_history,
        )
        if facts is None or facts.deleted:
            return None
        facts.running_tasks += 1
        # chat.send is a fallback for a lost typing-intent event.  It still
        # never waits for prefetch.
        if facts.has_completed_inference or facts.has_history:
            return self._request(facts, "prefetch")
        return None

    def task_finished(
        self,
        *,
        session_id: str,
        succeeded: bool,
    ) -> KVCGuardActionRequest | None:
        facts = self._facts.get(_normalize(session_id))
        if facts is None or facts.deleted:
            return None
        facts.running_tasks = max(0, facts.running_tasks - 1)
        if succeeded:
            facts.has_completed_inference = True
        return self._maybe_offload(facts)

    def delete(
        self,
        *,
        session_id: str,
        channel_id: str,
        is_team: bool,
    ) -> KVCGuardActionRequest | None:
        facts = self._ensure(
            session_id,
            channel_id=channel_id,
            is_team=is_team,
        )
        if facts is None or facts.deleted:
            return None
        facts.deleted = True
        facts.foreground_view_ids.clear()
        return self._request(facts, "evict")

    def restore_after_failed_delete(self, session_id: str) -> None:
        """Undo only the process-local tombstone when product deletion fails."""
        facts = self._facts.get(_normalize(session_id))
        if facts is None:
            return
        facts.deleted = False

    def _ensure(
        self,
        session_id: str,
        *,
        channel_id: str,
        is_team: bool,
        has_history: bool = False,
    ) -> SessionKVCFacts | None:
        normalized_session_id = _normalize(session_id)
        if not normalized_session_id or normalized_session_id == "new":
            return None
        facts = self._facts.get(normalized_session_id)
        if facts is None:
            facts = SessionKVCFacts(session_id=normalized_session_id)
            self._facts[normalized_session_id] = facts
        facts.channel_id = _normalize(channel_id) or facts.channel_id
        facts.is_team = bool(is_team)
        facts.has_history = facts.has_history or bool(has_history)
        return facts

    def _maybe_offload(
        self,
        facts: SessionKVCFacts,
    ) -> KVCGuardActionRequest | None:
        if facts.deleted:
            return None
        if facts.running_tasks > 0 or facts.foreground_view_ids:
            return None
        if not facts.has_completed_inference:
            return None
        return self._request(facts, "offload")

    @staticmethod
    def _request(
        facts: SessionKVCFacts,
        action: KVCGuardAction,
    ) -> KVCGuardActionRequest:
        return KVCGuardActionRequest(
            action=action,
            session_id=facts.session_id,
            channel_id=facts.channel_id,
            is_team=facts.is_team,
        )


def _normalize(value: object) -> str:
    return str(value or "").strip()


_TASK_GUARD = SessionKVCacheTaskGuard()


def get_session_kv_cache_task_guard() -> SessionKVCacheTaskGuard:
    return _TASK_GUARD
