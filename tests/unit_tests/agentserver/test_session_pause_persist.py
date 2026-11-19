# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-level is_paused persistence and pause-freeze history session_id."""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.agents.harness.team.team_manager import (
    TeamManager,
    _persist_session_paused_flag,
)


def test_persist_session_paused_flag_writes_metadata(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def _update(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.update_session_metadata",
        _update,
    )
    _persist_session_paused_flag("sess-pause-1", is_paused=True)
    assert calls == [
        {
            "session_id": "sess-pause-1",
            "is_paused": True,
            "touch_last_message_at": False,
            "sync_write": True,
        }
    ]

    calls.clear()
    _persist_session_paused_flag("sess-pause-1", is_paused=False)
    assert calls[0]["is_paused"] is False


@pytest.mark.asyncio
async def test_broadcast_running_task_freeze_history_includes_session_id(
    monkeypatch,
) -> None:
    appended: list[dict[str, Any]] = []

    class _Monitor:
        async def get_team_snapshot(self) -> dict[str, Any]:
            return {
                "team_id": "team-1",
                "tasks": [
                    {
                        "task_id": "t1-academic",
                        "status": "in_progress",
                        "title": "Academic",
                        "assignee": "assessment_specialist",
                    }
                ],
            }

    async def _broadcast(session_id: str, event: dict[str, Any], **_kwargs: Any) -> bool:
        del session_id, event
        return True

    def _append_history_record(**kwargs: Any) -> None:
        appended.append(kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        _append_history_record,
    )

    manager = TeamManager()
    manager._team_monitors["sess-freeze"] = _Monitor()
    manager.broadcast_event = _broadcast  # type: ignore[method-assign]

    await manager._broadcast_running_task_freeze("sess-freeze")

    assert len(appended) == 1
    extra = appended[0]["extra"]
    assert extra["session_id"] == "sess-freeze"
    assert extra["event"]["type"] == "team.task.paused"
    assert extra["event"]["progress_frozen"] is True
    assert extra["event"]["task_id"] == "t1-academic"
