from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.skilldev.schema import SkillDevState
from jiuwenclaw.agentserver.skilldev.session_history.agent_checkpoint import (
    build_agent_restore_payload,
    build_agent_session_summary,
    is_agent_checkpoint,
    merge_agent_checkpoint,
)
from jiuwenclaw.agentserver.skilldev.session_history.assembler import (
    build_restore_payload,
    build_session_summary,
    normalize_timeline,
)
from jiuwenclaw.agentserver.skilldev.session_history.store import SkillDevSessionHistoryStore
from jiuwenclaw.agentserver.skilldev.store import StateStore


class SkillDevSessionHistoryService:
    def __init__(self, *, base_dir: Path, state_store: StateStore) -> None:
        self._store = SkillDevSessionHistoryStore(base_dir)
        self._state_store = state_store

    def append_user_start(self, *, task_id: str, payload: dict[str, Any]) -> None:
        self._store.append_event(
            task_id=task_id,
            source="user",
            event_type="skilldev.user_start",
            payload=payload,
        )

    def append_user_chat(self, *, task_id: str, payload: dict[str, Any]) -> None:
        self._store.append_event(
            task_id=task_id,
            source="user",
            event_type="skilldev.user_chat",
            payload=payload,
        )

    def append_user_answer(self, *, task_id: str, payload: dict[str, Any]) -> None:
        self._store.append_event(
            task_id=task_id,
            source="user",
            event_type="skilldev.user_answer",
            payload=payload,
        )

    def append_user_respond(self, *, task_id: str, payload: dict[str, Any]) -> None:
        self._store.append_event(
            task_id=task_id,
            source="user",
            event_type="skilldev.user_respond",
            payload=payload,
        )

    def append_agent_event(self, *, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._store.append_event(
            task_id=task_id,
            source="assistant",
            event_type=event_type,
            payload=payload,
        )

    def save_state_snapshot(self, *, task_id: str, state: SkillDevState) -> None:
        self._store.save_snapshot(task_id, state.to_checkpoint_dict())

    def load_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        return self._state_store.load_checkpoint_dict(task_id)

    def save_agent_checkpoint(
        self,
        *,
        task_id: str,
        checkpoint: dict[str, Any],
    ) -> None:
        self._state_store.save_checkpoint_dict(task_id, checkpoint)
        self._store.save_snapshot(task_id, checkpoint)

    def finalize_agent_task(
        self,
        *,
        task_id: str,
        task_workspace: Path,
        mode: str = "create",
        conversation_id: str = "",
        error: str | None = None,
    ) -> None:
        events = self._store.list_events(task_id)
        existing = self._state_store.load_checkpoint_dict(task_id)
        round_count = sum(
            1
            for e in events
            if e.event_type in ("skilldev.user_start", "skilldev.user_chat")
        )
        checkpoint = merge_agent_checkpoint(
            existing if is_agent_checkpoint(existing or {}) else None,
            task_id=task_id,
            events=events,
            task_workspace=task_workspace,
            mode=str((existing or {}).get("mode") or mode),
            conversation_id=conversation_id,
            round_count=round_count,
            error=error,
        )
        self.save_agent_checkpoint(task_id=task_id, checkpoint=checkpoint)

    def list_sessions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for task_id in self._state_store.list_tasks():
            checkpoint = self._state_store.load_checkpoint_dict(task_id)
            if checkpoint is None:
                continue
            if is_agent_checkpoint(checkpoint):
                out.append(build_agent_session_summary(task_id, checkpoint))
            else:
                summary = build_session_summary(task_id, checkpoint)
                out.append(summary.to_dict())
        out.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return out

    def restore_session(self, task_id: str) -> dict[str, Any] | None:
        checkpoint = self._state_store.load_checkpoint_dict(task_id)
        if checkpoint is None:
            return None
        events = self._store.list_events(task_id)
        timeline_items = normalize_timeline(events)
        if is_agent_checkpoint(checkpoint):
            snapshot_file = self._store.load_snapshot(task_id)
            state_dict = (
                snapshot_file if isinstance(snapshot_file, dict) else checkpoint
            )
            return build_agent_restore_payload(
                task_id=task_id,
                checkpoint=state_dict,
                events=events,
                timeline_items=timeline_items,
            )
        state = SkillDevState.from_checkpoint_dict(checkpoint)
        snapshot = self._store.load_snapshot(task_id)
        state_dict = snapshot if isinstance(snapshot, dict) else state.to_checkpoint_dict()
        payload = build_restore_payload(
            task_id=task_id, state_dict=state_dict, events=events
        )
        payload["runner"] = "pipeline"
        return payload
