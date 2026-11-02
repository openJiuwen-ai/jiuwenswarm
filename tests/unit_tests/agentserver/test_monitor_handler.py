# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.agent_teams.monitor.models import MonitorEvent, MonitorEventType

from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
    _TASK_BODY_EVENT_TYPES,
    _TASK_TEXT_LIMIT,
    _task_text_field,
    _truncate_task_text,
)
from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler


class _FakeMessage:
    def __init__(
        self,
        message_id: str,
        content: str,
        protocol: str = "plain",
    ) -> None:
        self.message_id = message_id
        self.content = content
        self.protocol = protocol


class _FakeMember:
    def __init__(self, member_name: str, display_name: str = "", status: str = "ready",
                 execution_status: str | None = None, mode: str = "normal", role: str = "teammate"):
        self.member_name = member_name
        self.display_name = display_name
        self.status = status
        self.execution_status = execution_status
        self.mode = mode
        self.role = role


class _FakeTask:
    def __init__(self, task_id: str = "task-1", title: str = "test task",
                 content: str = "do something", status: str = "created",
                 assignee: str | None = None, updated_at: int | None = None):
        self.task_id = task_id
        self.team_name = "team-1"
        self.title = title
        self.content = content
        self.status = status
        self.assignee = assignee
        self.updated_at = updated_at


class _FakeTaskDao:
    """Stand-in for ``TaskDAO`` exposing only ``get_task`` used by _lookup_task_body."""

    def __init__(self, task: _FakeTask | None = None, *, get_task_mock=None) -> None:
        self._task = task
        self._explicit_mock = get_task_mock
        # Call counter so tests can assert "NOT called" for status-only events.
        self.get_task_call_count = 0

    async def get_task(self, task_id: str):
        self.get_task_call_count += 1
        if self._explicit_mock is not None:
            return await self._explicit_mock(task_id)
        return self._task


class _FakeDb:
    """Stand-in for ``monitor._db`` exposing only ``.task`` (a _FakeTaskDao)."""

    def __init__(self, task: _FakeTaskDao) -> None:
        self.task = task


class _FakeMonitor:
    def __init__(
        self,
        members: list[_FakeMember],
        leader_member_name: str | None,
        events: list[MonitorEvent] | None = None,
        tasks: list[_FakeTask] | None = None,
        messages: list[_FakeMessage] | None = None,
        task_dao: _FakeTaskDao | None = None,
    ):
        self.team_name = "team-1"
        self._members = members
        self._leader_member_name = leader_member_name
        self._events = events or []
        self._tasks = tasks or []
        self._messages = messages or []
        # ``get_task`` (the public monitor API used by _lookup_task_body)
        # delegates to this DAO. When no DAO is injected the real monitor would
        # raise (no ``_db``), which _lookup_task_body catches and degrades to a
        # body-less event; the fake reproduces that by raising in ``get_task``.
        self._task_dao = task_dao
        if task_dao is not None:
            self._db = _FakeDb(task_dao)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def events(self):
        for event in self._events:
            yield event

    async def get_members(self) -> list[_FakeMember]:
        return list(self._members)

    async def get_member(self, member_name: str, team_name: str | None = None) -> _FakeMember | None:
        for m in self._members:
            if m.member_name == member_name:
                return m
        return None

    async def get_team_info(self):
        if self._leader_member_name is None:
            return None
        return SimpleNamespace(leader_member_name=self._leader_member_name)

    async def get_tasks(self) -> list[_FakeTask]:
        return list(self._tasks)

    async def get_task(self, task_id: str):
        """Mirror TeamMonitor.get_task: single-row read via the DAO.

        When no DAO was injected the real monitor would raise (no ``_db``),
        which _lookup_task_body catches and degrades to a body-less event.
        """
        if self._task_dao is None:
            raise AttributeError("_db not configured")
        return await self._task_dao.get_task(task_id)

    async def get_messages(self) -> list[_FakeMessage]:
        return list(self._messages)

    @contextlib.contextmanager
    def _bound_session(self):
        """Mirror TeamMonitor._bound_session (binds session_id contextvar).

        The real one sets/resets a contextvar for per-session table routing; the
        fake has no contextvar so it's a no-op contextmanager — sufficient to
        exercise the ``with self._monitor._bound_session():`` code path.
        """
        yield


@pytest.mark.anyio
async def test_get_team_snapshot_filters_leader_member() -> None:
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[_FakeMember("team_leader"), _FakeMember("worker-1")],
            leader_member_name="team_leader",
            tasks=[_FakeTask(task_id="task-1", title="research", status="created", assignee="worker-1")],
        ),
        "sess-1",
    )

    snapshot = await handler.get_team_snapshot()

    assert snapshot == {
        "members": [
            {
                "member_id": "worker-1",
                "name": "",
                "status": "ready",
                "execution_status": None,
                "mode": "normal",
                "role": "teammate",
            }
        ],
        "tasks": [
            {
                "task_id": "task-1",
                "team_name": "team-1",
                "title": "research",
                "content": "do something",
                "status": "created",
                "assignee": "worker-1",
                "updated_at": None,
            }
        ],
        "team_id": "team-1",
    }


@pytest.mark.anyio
async def test_get_team_snapshot_keeps_members_when_team_info_unavailable() -> None:
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[_FakeMember("worker-1"), _FakeMember("worker-2")],
            leader_member_name=None,
        ),
        "sess-2",
    )

    snapshot = await handler.get_team_snapshot()

    assert snapshot == {
        "members": [
            {
                "member_id": "worker-1",
                "name": "",
                "status": "ready",
                "execution_status": None,
                "mode": "normal",
                "role": "teammate",
            },
            {
                "member_id": "worker-2",
                "name": "",
                "status": "ready",
                "execution_status": None,
                "mode": "normal",
                "role": "teammate",
            },
        ],
        "tasks": [],
        "team_id": "team-1",
    }


@pytest.mark.anyio
async def test_convert_event_includes_session_id() -> None:
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-1",
        status="created",
    )
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[],
            leader_member_name=None,
            events=[event],
        ),
        "sess-monitor",
    )

    await handler.start()
    try:
        converted = await anext(handler.events())

        assert converted == {
            "event_type": "team.task",
            "session_id": "sess-monitor",
            "event": {
                "type": "team.task.created",
                "team_id": "team-1",
                "task_id": "task-1",
                "status": "created",
            },
        }
    finally:
        await handler.stop()


@pytest.mark.anyio
async def test_convert_json_protocol_message_decodes_unicode_escapes() -> None:
    event = MonitorEvent(
        event_type=MonitorEventType.MESSAGE,
        team_name="team-1",
        timestamp=123,
        message_id="msg-approval",
        from_member_name="team_leader",
        to_member_name="worker-1",
    )
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[],
            leader_member_name=None,
            events=[event],
            messages=[
                _FakeMessage(
                    message_id="msg-approval",
                    content=(
                        '{"type": "tool_approval_result", "approved": true, '
                        '"feedback": "\\u597d\\u8bd7"}'
                    ),
                    protocol="json",
                ),
            ],
        ),
        "sess-monitor",
    )

    await handler.start()
    try:
        converted = await anext(handler.events())

        assert converted["event"]["protocol"] == "json"
        assert converted["event"]["content"] == (
            '{"type": "tool_approval_result", "approved": true, "feedback": "好诗"}'
        )
    finally:
        await handler.stop()


@pytest.mark.anyio
async def test_convert_plain_protocol_message_keeps_json_like_text_unchanged() -> None:
    raw_content = '{"feedback": "\\u597d\\u8bd7"}'
    event = MonitorEvent(
        event_type=MonitorEventType.BROADCAST,
        team_name="team-1",
        timestamp=123,
        message_id="msg-plain",
        from_member_name="team_leader",
    )
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[],
            leader_member_name=None,
            events=[event],
            messages=[
                _FakeMessage(
                    message_id="msg-plain",
                    content=raw_content,
                    protocol="plain",
                ),
            ],
        ),
        "sess-monitor",
    )

    await handler.start()
    try:
        converted = await anext(handler.events())

        assert converted["event"]["protocol"] == "plain"
        assert converted["event"]["content"] == raw_content
    finally:
        await handler.stop()


@pytest.mark.anyio
async def test_member_spawned_event_includes_mode_for_human_agent() -> None:
    """验证 member_spawned 事件对 human_agent 成员返回 mode="human"."""
    event = MonitorEvent(
        event_type=MonitorEventType.MEMBER_SPAWNED,
        team_name="team-1",
        timestamp=123,
        member_name="HumanPlayer_A",
    )
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[_FakeMember("HumanPlayer_A", role="human_agent")],
            leader_member_name=None,
            events=[event],
        ),
        "sess-monitor",
    )

    await handler.start()
    try:
        converted = await anext(handler.events())

        assert converted["event"]["member_id"] == "HumanPlayer_A"
        assert converted["event"]["mode"] == "human"
    finally:
        await handler.stop()


@pytest.mark.anyio
async def test_member_spawned_event_includes_mode_for_ai_member() -> None:
    """验证 member_spawned 事件对 AI 成员返回 mode=role（teammate/leader 等）。"""
    event = MonitorEvent(
        event_type=MonitorEventType.MEMBER_SPAWNED,
        team_name="team-1",
        timestamp=123,
        member_name="Werewolf_AI_1",
    )
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[_FakeMember("Werewolf_AI_1", role="teammate")],
            leader_member_name=None,
            events=[event],
        ),
        "sess-monitor",
    )

    await handler.start()
    try:
        converted = await anext(handler.events())

        assert converted["event"]["member_id"] == "Werewolf_AI_1"
        # AI 成员（role=teammate）返回 mode=role 值
        assert converted["event"]["mode"] == "teammate"
    finally:
        await handler.stop()


# =====================================================================
# Truncation helpers (pure functions)
# =====================================================================


class TestTruncateTaskText:
    """``_truncate_task_text`` returns (truncated_str, was_truncated, original_size)."""

    def test_non_string_returns_passthrough_untruncated(self) -> None:
        assert _truncate_task_text(None) == (None, False, 0)
        assert _truncate_task_text(123) == (123, False, 0)

    def test_empty_string_returns_empty_untruncated(self) -> None:
        assert _truncate_task_text("") == ("", False, 0)

    def test_under_limit_returns_unchanged_untruncated(self) -> None:
        value = "a" * _TASK_TEXT_LIMIT
        result, truncated, original = _truncate_task_text(value)
        assert result == value
        assert truncated is False
        assert original == _TASK_TEXT_LIMIT

    def test_exactly_at_limit_is_not_truncated(self) -> None:
        value = "b" * _TASK_TEXT_LIMIT
        _, truncated, original = _truncate_task_text(value)
        assert truncated is False
        assert original == _TASK_TEXT_LIMIT

    def test_over_limit_truncates_with_inline_marker(self) -> None:
        value = "c" * (_TASK_TEXT_LIMIT + 50)
        result, truncated, original = _truncate_task_text(value)
        assert truncated is True
        assert original == _TASK_TEXT_LIMIT + 50
        # Truncated string = first _TASK_TEXT_LIMIT chars + inline marker; marker uses U+2026
        # ellipsis and reports the ORIGINAL length of this field.
        assert result == "c" * _TASK_TEXT_LIMIT + f"…(truncated, total {original} chars)"
        # Inline marker must use the Unicode ellipsis (U+2026), not three dots.
        assert "…" in result
        assert "..." not in result

    def test_over_limit_truncates_at_exactly_one_over(self) -> None:
        value = "d" * (_TASK_TEXT_LIMIT + 1)
        result, truncated, original = _truncate_task_text(value)
        assert truncated is True
        assert original == _TASK_TEXT_LIMIT + 1
        assert result == "d" * _TASK_TEXT_LIMIT + f"…(truncated, total {original} chars)"


class TestTaskTextField:
    """``_task_text_field`` expands to a dict; flags attach ONLY when truncated."""

    def test_under_limit_emits_only_value_key(self) -> None:
        out = _task_text_field("title", "short title")
        assert out == {"title": "short title"}
        # No flag keys when not truncated.
        assert "title_truncated" not in out
        assert "title_original_size" not in out

    def test_over_limit_attaches_truncation_flags(self) -> None:
        value = "x" * (_TASK_TEXT_LIMIT + 10)
        out = _task_text_field("content", value)
        assert out["content"] == "x" * _TASK_TEXT_LIMIT + f"…(truncated, total {len(value)} chars)"
        assert out["content_truncated"] is True
        assert out["content_original_size"] == len(value)

    def test_non_string_emits_only_value_key(self) -> None:
        out = _task_text_field("title", None)
        assert out == {"title": None}
        assert "title_truncated" not in out
        assert "title_original_size" not in out

    def test_empty_string_emits_only_value_key(self) -> None:
        out = _task_text_field("title", "")
        assert out == {"title": ""}
        assert "title_truncated" not in out


# =====================================================================
# _handle_task: body supplement + truncation + non-body events
# =====================================================================


def _make_handler(task_dao: _FakeTaskDao | None = None, *, session_id: str = "sess-1") -> TeamMonitorHandler:
    return TeamMonitorHandler(
        _FakeMonitor(members=[], leader_member_name=None, task_dao=task_dao),
        session_id,
    )


@pytest.mark.anyio
async def test_handle_task_created_supplements_title_and_content_from_db() -> None:
    """TASK_CREATED re-queries the DB and attaches title/content to the event."""
    task_dao = _FakeTaskDao(
        _FakeTask(task_id="task-1", title="research plan", content="do the thing")
    )
    handler = _make_handler(task_dao)
    base = {"type": "team.task.created", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-1",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert result["task_id"] == "task-1"
    assert result["status"] == "pending"
    assert result["title"] == "research plan"
    assert result["content"] == "do the thing"
    # Under limit → no truncation flags.
    assert "title_truncated" not in result
    assert "content_truncated" not in result
    assert task_dao.get_task_call_count == 1


@pytest.mark.anyio
async def test_handle_task_created_backfills_assignee_from_db() -> None:
    """TASK_CREATED carries no member_name; assignee is backfilled from the DB.

    Lets the frontend attach a freshly-created task to its assignee's card
    without waiting for a later TASK_CLAIMED event.
    """
    task_dao = _FakeTaskDao(
        _FakeTask(task_id="task-1", title="research", content="do", assignee="worker-1")
    )
    handler = _make_handler(task_dao)
    base = {"type": "team.task.created", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-1",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert result["assignee"] == "worker-1"
    assert "member_id" not in result
    assert task_dao.get_task_call_count == 1


@pytest.mark.anyio
async def test_handle_task_created_no_assignee_when_db_task_unassigned() -> None:
    """An unassigned DB task must not synthesize an assignee field."""
    task_dao = _FakeTaskDao(
        _FakeTask(task_id="task-1", title="research", content="do", assignee=None)
    )
    handler = _make_handler(task_dao)
    base = {"type": "team.task.created", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-1",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert "assignee" not in result


@pytest.mark.anyio
async def test_handle_task_updated_supplements_body_from_db() -> None:
    """TASK_UPDATED also triggers the DB body lookup."""
    task_dao = _FakeTaskDao(
        _FakeTask(task_id="task-2", title="updated title", content="updated content")
    )
    handler = _make_handler(task_dao)
    base = {"type": "team.task.updated", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_UPDATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-2",
        status="in_progress",
    )

    result = await handler._handle_task(base, event)

    assert result["title"] == "updated title"
    assert result["content"] == "updated content"
    assert result["status"] == "in_progress"
    assert task_dao.get_task_call_count == 1


@pytest.mark.anyio
async def test_handle_task_created_truncates_over_limit_body() -> None:
    """When title/content exceed the limit, the event carries truncation flags."""
    long_title = "T" * (_TASK_TEXT_LIMIT + 20)
    long_content = "C" * (_TASK_TEXT_LIMIT + 100)
    task_dao = _FakeTaskDao(
        _FakeTask(task_id="task-3", title=long_title, content=long_content)
    )
    handler = _make_handler(task_dao)
    base = {"type": "team.task.created", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-3",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert result["title"] == "T" * _TASK_TEXT_LIMIT + f"…(truncated, total {len(long_title)} chars)"
    assert result["title_truncated"] is True
    assert result["title_original_size"] == len(long_title)
    assert result["content"] == "C" * _TASK_TEXT_LIMIT + f"…(truncated, total {len(long_content)} chars)"
    assert result["content_truncated"] is True
    assert result["content_original_size"] == len(long_content)


@pytest.mark.anyio
async def test_handle_task_claimed_does_not_query_db() -> None:
    """A status-only event (TASK_CLAIMED) MUST NOT trigger a DB body lookup."""
    task_dao = _FakeTaskDao(_FakeTask(task_id="task-4"))
    handler = _make_handler(task_dao)
    base = {"type": "team.task.claimed", "team_id": "team-1", "member_id": "worker-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CLAIMED,
        team_name="team-1",
        timestamp=123,
        task_id="task-4",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert result["task_id"] == "task-4"
    assert result["status"] == "in_progress"
    assert "title" not in result
    assert "content" not in result
    assert task_dao.get_task_call_count == 0


@pytest.mark.anyio
async def test_handle_task_emits_task_id_and_status_when_lookup_raises() -> None:
    """If the DB lookup raises, the event still carries task_id + status (no body)."""
    failing_mock = AsyncMock(side_effect=RuntimeError("db down"))
    task_dao = _FakeTaskDao(get_task_mock=failing_mock)
    handler = _make_handler(task_dao)
    base = {"type": "team.task.created", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-5",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert result["task_id"] == "task-5"
    assert result["status"] == "pending"
    assert "title" not in result
    assert "content" not in result


@pytest.mark.anyio
async def test_handle_task_emits_task_id_and_status_when_task_not_found() -> None:
    """If get_task returns None (task not in DB), event carries task_id + status only."""
    task_dao = _FakeTaskDao(task=None)
    handler = _make_handler(task_dao)
    base = {"type": "team.task.created", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-missing",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert result["task_id"] == "task-missing"
    assert result["status"] == "pending"
    assert "title" not in result
    assert "content" not in result
    assert task_dao.get_task_call_count == 1


@pytest.mark.anyio
async def test_handle_task_emits_task_id_and_status_when_monitor_lacks_db() -> None:
    """If monitor has no _db (AttributeError), event still carries task_id + status."""
    # No task_dao injected → _FakeMonitor has no _db attribute → AttributeError,
    # caught by _lookup_task_body, degrades to no-body event.
    handler = TeamMonitorHandler(
        _FakeMonitor(members=[], leader_member_name=None),
        "sess-1",
    )
    base = {"type": "team.task.created", "team_id": "team-1"}
    event = MonitorEvent(
        event_type=MonitorEventType.TASK_CREATED,
        team_name="team-1",
        timestamp=123,
        task_id="task-6",
        status=None,
    )

    result = await handler._handle_task(base, event)

    assert result["task_id"] == "task-6"
    assert result["status"] == "pending"
    assert "title" not in result
    assert "content" not in result


def test_task_body_event_types_contains_only_created_and_updated() -> None:
    """Only TASK_CREATED and TASK_UPDATED trigger the DB body lookup."""
    assert _TASK_BODY_EVENT_TYPES == {
        MonitorEventType.TASK_CREATED,
        MonitorEventType.TASK_UPDATED,
    }


# =====================================================================
# get_team_snapshot: title/content truncation (T2.1)
#
# ``get_team_snapshot`` feeds BOTH the ``team.task.status_snapshot`` live
# broadcast (emitted by ``_broadcast_team_state_snapshot``) AND the
# ``team.snapshot`` RPC (served by ``agent_ws_server._handle_team_snapshot``).
# So applying truncation here covers both exits (design D4).
# =====================================================================


@pytest.mark.anyio
async def test_get_team_snapshot_truncates_over_limit_title_and_keeps_under_limit_content() -> None:
    """A task whose title > limit gets a truncated title + flags; content ≤ limit
    gets no flags (and the content_truncated key MUST be absent)."""
    long_title = "T" * (_TASK_TEXT_LIMIT + 30)
    short_content = "do the research"
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[_FakeMember("worker-1")],
            leader_member_name=None,
            tasks=[
                _FakeTask(
                    task_id="task-long",
                    title=long_title,
                    content=short_content,
                    status="in_progress",
                    assignee="worker-1",
                )
            ],
        ),
        "sess-snap-1",
    )

    snapshot = await handler.get_team_snapshot()
    assert snapshot is not None
    task = snapshot["tasks"][0]

    # title is truncated to first _TASK_TEXT_LIMIT + inline marker carrying ORIGINAL size.
    assert task["title"] == "T" * _TASK_TEXT_LIMIT + f"…(truncated, total {len(long_title)} chars)"
    assert task["title_truncated"] is True
    assert task["title_original_size"] == len(long_title)
    # content under the limit → no truncation flags, and the flag keys must
    # be ABSENT (not just falsy).
    assert task["content"] == short_content
    assert "content_truncated" not in task
    assert "content_original_size" not in task
    # Non-body fields untouched.
    assert task["task_id"] == "task-long"
    assert task["status"] == "in_progress"
    assert task["assignee"] == "worker-1"


@pytest.mark.anyio
async def test_get_team_snapshot_truncates_over_limit_content_and_attaches_flags() -> None:
    """A task whose content > limit gets a truncated content + flags."""
    short_title = "research plan"
    long_content = "C" * (_TASK_TEXT_LIMIT + 75)
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[_FakeMember("worker-1")],
            leader_member_name=None,
            tasks=[
                _FakeTask(
                    task_id="task-c",
                    title=short_title,
                    content=long_content,
                    status="pending",
                )
            ],
        ),
        "sess-snap-2",
    )

    snapshot = await handler.get_team_snapshot()
    assert snapshot is not None
    task = snapshot["tasks"][0]

    assert task["title"] == short_title
    assert "title_truncated" not in task
    assert task["content"] == "C" * _TASK_TEXT_LIMIT + f"…(truncated, total {len(long_content)} chars)"
    assert task["content_truncated"] is True
    assert task["content_original_size"] == len(long_content)


@pytest.mark.anyio
async def test_get_team_snapshot_under_limit_title_and_content_emit_no_flag_keys() -> None:
    """Both title and content ≤ limit → no flag keys at all."""
    handler = TeamMonitorHandler(
        _FakeMonitor(
            members=[_FakeMember("worker-1")],
            leader_member_name=None,
            tasks=[
                _FakeTask(
                    task_id="task-short",
                    title="short title",
                    content="short content",
                    status="completed",
                )
            ],
        ),
        "sess-snap-3",
    )

    snapshot = await handler.get_team_snapshot()
    assert snapshot is not None
    task = snapshot["tasks"][0]

    assert task["title"] == "short title"
    assert task["content"] == "short content"
    assert "title_truncated" not in task
    assert "title_original_size" not in task
    assert "content_truncated" not in task
    assert "content_original_size" not in task


# ---------------------------------------------------------------------------
# get_team_snapshot_from_db — monitor-down history restore path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_team_snapshot_from_db_requires_session_and_team() -> None:
    assert await TeamMonitorHandler.get_team_snapshot_from_db("", "team-1") is None
    assert await TeamMonitorHandler.get_team_snapshot_from_db("sess-1", "") is None


@pytest.mark.anyio
@pytest.mark.parametrize("stored_status,expected_status", [
    ("pending", "pending"),
    ("in_progress", "in_progress"),
    ("completed", "completed"),
    ("cancelled", "cancelled"),
    ("claimed", "in_progress"),
    ("started", "in_progress"),
    ("plan_approved", "in_progress"),
])
async def test_get_team_snapshot_from_db_reads_tasks_with_truncation(
    stored_status: str, expected_status: str,
) -> None:
    """When monitor is down, bind session_id and read tasks/members from team.db."""
    from pathlib import Path
    from unittest.mock import patch

    long_title = "T" * (_TASK_TEXT_LIMIT + 20)
    member = _FakeMember("worker-1", display_name="Worker")
    leader = _FakeMember("team-leader", role="leader")
    task = _FakeTask(
        task_id="uuid-1",
        title=long_title,
        content="body",
        status=stored_status,
        assignee="worker-1",
        updated_at=123,
    )

    class _FakeTeamInfo:
        leader_member_name = "team-leader"

    class _FakeSharedDb:
        def __init__(self) -> None:
            self.member = SimpleNamespace(
                get_team_members=AsyncMock(return_value=[leader, member])
            )
            self.task = SimpleNamespace(
                get_team_tasks=AsyncMock(return_value=[task]),
                get_team_dependencies=AsyncMock(return_value=[
                    SimpleNamespace(task_id="b", depends_on_task_id="a", resolved=True)
                ])
            )
            self.team = SimpleNamespace(
                get_team=AsyncMock(return_value=_FakeTeamInfo())
            )
            self.initialize = AsyncMock()
            self.create_cur_session_tables = AsyncMock()

    shared = _FakeSharedDb()
    bound: list[str] = []

    def _fake_set_session_id(sid: str):
        bound.append(sid)
        return "token"

    with (
        patch(
            "jiuwenswarm.agents.harness.team.config_loader.resolve_team_sqlite_db_path",
            return_value=Path("/tmp/team.db"),
        ),
        patch("jiuwenswarm.common.config.get_config", return_value={}),
        patch(
            "openjiuwen.agent_teams.spawn.shared_resources.get_shared_db",
            return_value=shared,
        ),
        patch(
            "openjiuwen.agent_teams.tools.database.config.DatabaseConfig",
            return_value=SimpleNamespace(),
        ),
        patch(
            "openjiuwen.agent_teams.context.set_session_id",
            side_effect=_fake_set_session_id,
        ),
        patch(
            "openjiuwen.agent_teams.context.reset_session_id",
            return_value=None,
        ),
    ):
        snapshot = await TeamMonitorHandler.get_team_snapshot_from_db(
            "sess-19f8", "team-sess-1"
        )

        edges = await TeamMonitorHandler.get_team_dependencies_from_db("sess-19f8", "team-sess-1")

    assert edges == [{"task_id": "b", "depends_on_task_id": "a", "resolved": True}]
    assert snapshot is not None
    assert bound == ["sess-19f8", "sess-19f8"]
    shared.initialize.assert_awaited()
    shared.create_cur_session_tables.assert_not_awaited()
    assert snapshot["team_id"] == "team-sess-1"
    assert [m["member_id"] for m in snapshot["members"]] == ["worker-1"]
    assert len(snapshot["tasks"]) == 1
    out = snapshot["tasks"][0]
    assert out["task_id"] == "uuid-1"
    assert out["status"] == expected_status
    assert task.status == stored_status
    assert out["assignee"] == "worker-1"
    assert out["title_truncated"] is True
    assert out["title"].startswith("T" * _TASK_TEXT_LIMIT)
    assert out["content"] == "body"
    assert "content_truncated" not in out
