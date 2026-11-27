# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent 模式 SkillDev 会话历史记录（写入 session_history + state.json）."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.skilldev.session_history.service import SkillDevSessionHistoryService

logger = logging.getLogger(__name__)

_STREAM_MERGE_TYPES = frozenset({
    "skilldev.agent_output",
    "skilldev.agent_thinking",
})

_SKIP_PERSIST_TYPES = frozenset({
    "chat.usage_metadata",
})


class AgentSessionRecorder:
    """在 skilldev.chat 流式执行期间追加事件并维护 Agent checkpoint."""

    def __init__(self, service: SkillDevSessionHistoryService) -> None:
        self._service = service
        self._pending_type: str | None = None
        self._pending_payload: dict[str, Any] | None = None

    def begin_round(
        self,
        *,
        task_id: str,
        params: dict[str, Any],
        session_id: str,
        is_first: bool,
    ) -> None:
        self._flush_pending(task_id)
        if is_first:
            payload = {
                "task_id": task_id,
                "session_id": session_id,
                "query": str(params.get("message") or params.get("query") or ""),
                "files": params.get("files") or [],
                "skill_packages": params.get("skill_packages") or [],
                "tool_spec_files": params.get("tool_spec_files") or [],
                "agent_definitions": params.get("agent_definitions") or [],
                "cli_definitions": params.get("cli_definitions") or [],
            }
            self._service.append_user_start(task_id=task_id, payload=payload)
        else:
            self._service.append_user_chat(
                task_id=task_id,
                payload={
                    "task_id": task_id,
                    "session_id": session_id,
                    "message": str(params.get("message") or params.get("query") or ""),
                    "query": str(params.get("message") or params.get("query") or ""),
                },
            )

    def record_user_answer(self, *, task_id: str, payload: dict[str, Any]) -> None:
        self._flush_pending(task_id)
        self._service.append_user_answer(task_id=task_id, payload=payload)

    def record_payload(self, *, task_id: str, payload: dict[str, Any] | None) -> None:
        if not payload or not isinstance(payload, dict):
            return
        event_type = str(payload.get("event_type") or "")
        if not event_type or event_type in _SKIP_PERSIST_TYPES:
            return
        if event_type == "todo.updated":
            normalized = dict(payload)
            normalized["event_type"] = "skilldev.todos_update"
            self._append_or_merge(task_id=task_id, event_type="skilldev.todos_update", payload=normalized)
            return
        if event_type in _STREAM_MERGE_TYPES:
            delta = str(payload.get("delta") or "")
            if not delta:
                return
            if self._pending_type == event_type and self._pending_payload is not None:
                self._pending_payload["delta"] = str(self._pending_payload.get("delta") or "") + delta
                if payload.get("task_id"):
                    self._pending_payload["task_id"] = payload["task_id"]
                return
            self._flush_pending(task_id)
            self._pending_type = event_type
            self._pending_payload = dict(payload)
            return
        self._flush_pending(task_id)
        self._service.append_agent_event(
            task_id=task_id,
            event_type=event_type,
            payload=dict(payload),
        )

    def record_chunk(self, *, task_id: str, chunk: Any) -> None:
        payload = getattr(chunk, "payload", None)
        self.record_payload(task_id=task_id, payload=payload)

    def finalize(
        self,
        *,
        task_id: str,
        task_workspace: Path,
        conversation_id: str = "",
        mode: str = "create",
        error: str | None = None,
    ) -> None:
        self._flush_pending(task_id)
        try:
            self._service.finalize_agent_task(
                task_id=task_id,
                task_workspace=task_workspace,
                mode=mode,
                conversation_id=conversation_id,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[session=%s] [AgentSessionRecorder] finalize failed: %s",
                task_id,
                exc,
            )

    def _append_or_merge(
        self,
        *,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self._service.append_agent_event(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
        )

    def _flush_pending(self, task_id: str) -> None:
        if self._pending_type and self._pending_payload:
            self._service.append_agent_event(
                task_id=task_id,
                event_type=self._pending_type,
                payload=dict(self._pending_payload),
            )
        self._pending_type = None
        self._pending_payload = None
