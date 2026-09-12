# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for TeamMonitorHandler checkpoint and text utilities.

Covers:
- Checkpoint save (atomic write via tmp+rename)
- Checkpoint load (valid, invalid, missing, version mismatch)
- Text truncation (_truncate_task_text)
- Text field helper (_task_text_field)
- Environment variable resolution (_resolve_task_text_limit)
- Message normalization helpers
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
    TeamMonitorHandler,
    _resolve_task_text_limit,
    _truncate_task_text,
    _task_text_field,
    _TASK_TEXT_LIMIT_DEFAULT,
    _TASK_EVENT_STATUS,
)


# ===========================================================================
# Tests: _truncate_task_text
# ===========================================================================

class TestTruncateTaskText:
    """Test the _truncate_task_text utility function."""

    def test_none_input(self):
        """None input returns (None, False, 0)."""
        result, truncated, size = _truncate_task_text(None)
        assert result is None
        assert truncated is False
        assert size == 0

    def test_empty_string(self):
        """Empty string returns ('', False, 0)."""
        result, truncated, size = _truncate_task_text("")
        assert result == ""
        assert truncated is False
        assert size == 0

    def test_non_string_input(self):
        """Non-string input returns as-is with no truncation."""
        result, truncated, size = _truncate_task_text(12345)
        assert result == 12345
        assert truncated is False
        assert size == 0

    def test_short_string_no_truncation(self):
        """String under limit is returned unchanged."""
        text = "Short task title"
        result, truncated, size = _truncate_task_text(text)
        assert result == text
        assert truncated is False
        assert size == len(text)

    def test_exact_limit_no_truncation(self):
        """String exactly at limit is not truncated."""
        text = "x" * _TASK_TEXT_LIMIT_DEFAULT
        result, truncated, size = _truncate_task_text(text)
        assert result == text
        assert truncated is False
        assert size == _TASK_TEXT_LIMIT_DEFAULT

    def test_over_limit_truncated(self):
        """String over limit is truncated with marker."""
        text = "x" * (_TASK_TEXT_LIMIT_DEFAULT + 100)
        result, truncated, size = _truncate_task_text(text)
        assert truncated is True
        assert size == len(text)
        assert len(result) < len(text)
        assert "truncated" in result
        assert str(len(text)) in result

    def test_truncation_preserves_prefix(self):
        """Truncated text starts with the original prefix."""
        text = "ABCDEFGHIJ" + "x" * (_TASK_TEXT_LIMIT_DEFAULT + 100)
        result, truncated, size = _truncate_task_text(text)
        assert truncated is True
        assert result.startswith("ABCDEFGHIJ")


# ===========================================================================
# Tests: _task_text_field
# ===========================================================================

class TestTaskTextField:
    """Test the _task_text_field helper."""

    def test_short_value_no_flags(self):
        """Short value produces only the named key."""
        field = _task_text_field("title", "My Task")
        assert field == {"title": "My Task"}
        assert "title_truncated" not in field
        assert "title_original_size" not in field

    def test_none_value(self):
        """None value produces only the named key with None."""
        field = _task_text_field("content", None)
        assert field == {"content": None}

    def test_over_limit_adds_flags(self):
        """Over-limit value adds truncated and original_size flags."""
        long_text = "x" * (_TASK_TEXT_LIMIT_DEFAULT + 500)
        field = _task_text_field("content", long_text)
        assert "content" in field
        assert field["content_truncated"] is True
        assert field["content_original_size"] == len(long_text)

    def test_empty_string(self):
        """Empty string produces only the named key."""
        field = _task_text_field("title", "")
        assert field == {"title": ""}


# ===========================================================================
# Tests: _resolve_task_text_limit
# ===========================================================================

class TestResolveTaskTextLimit:
    """Test environment variable resolution for task text limit."""

    def test_default_when_env_unset(self):
        """Returns default when env var is not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JIUWENSWARM_TASK_TEXT_LIMIT", None)
            assert _resolve_task_text_limit() == _TASK_TEXT_LIMIT_DEFAULT

    def test_default_when_env_blank(self):
        """Returns default when env var is blank."""
        with patch.dict(os.environ, {"JIUWENSWARM_TASK_TEXT_LIMIT": ""}):
            assert _resolve_task_text_limit() == _TASK_TEXT_LIMIT_DEFAULT

    def test_default_when_env_whitespace(self):
        """Returns default when env var is whitespace."""
        with patch.dict(os.environ, {"JIUWENSWARM_TASK_TEXT_LIMIT": "   "}):
            assert _resolve_task_text_limit() == _TASK_TEXT_LIMIT_DEFAULT

    def test_custom_valid_value(self):
        """Valid positive integer is returned."""
        with patch.dict(os.environ, {"JIUWENSWARM_TASK_TEXT_LIMIT": "8192"}):
            assert _resolve_task_text_limit() == 8192

    def test_invalid_non_integer(self):
        """Non-integer value falls back to default."""
        with patch.dict(os.environ, {"JIUWENSWARM_TASK_TEXT_LIMIT": "abc"}):
            assert _resolve_task_text_limit() == _TASK_TEXT_LIMIT_DEFAULT

    def test_invalid_zero(self):
        """Zero value falls back to default."""
        with patch.dict(os.environ, {"JIUWENSWARM_TASK_TEXT_LIMIT": "0"}):
            assert _resolve_task_text_limit() == _TASK_TEXT_LIMIT_DEFAULT

    def test_invalid_negative(self):
        """Negative value falls back to default."""
        with patch.dict(os.environ, {"JIUWENSWARM_TASK_TEXT_LIMIT": "-100"}):
            assert _resolve_task_text_limit() == _TASK_TEXT_LIMIT_DEFAULT

    def test_small_valid_value(self):
        """Small positive integer is accepted."""
        with patch.dict(os.environ, {"JIUWENSWARM_TASK_TEXT_LIMIT": "1"}):
            assert _resolve_task_text_limit() == 1


# ===========================================================================
# Tests: Checkpoint load
# ===========================================================================

class TestCheckpointLoad:
    """Test TeamMonitorHandler.load_task_checkpoint static method."""

    def test_load_valid_checkpoint(self, tmp_path):
        """Valid checkpoint file is loaded correctly."""
        checkpoint = {
            "version": 1,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "team_name": "test-team",
            "session_id": "sess-123",
            "members": [{"member_name": "m1", "role": "teammate"}],
            "tasks": [{"task_id": "t1", "status": "pending"}],
        }
        cp_file = tmp_path / "checkpoint.json"
        cp_file.write_text(json.dumps(checkpoint), encoding="utf-8")

        result = TeamMonitorHandler.load_task_checkpoint(str(tmp_path))
        assert result is not None
        assert result["version"] == 1
        assert result["team_name"] == "test-team"
        assert len(result["members"]) == 1
        assert len(result["tasks"]) == 1

    def test_load_missing_checkpoint(self, tmp_path):
        """Missing checkpoint file returns None."""
        result = TeamMonitorHandler.load_task_checkpoint(str(tmp_path))
        assert result is None

    def test_load_invalid_json(self, tmp_path):
        """Invalid JSON returns None."""
        cp_file = tmp_path / "checkpoint.json"
        cp_file.write_text("not valid json {{{", encoding="utf-8")

        result = TeamMonitorHandler.load_task_checkpoint(str(tmp_path))
        assert result is None

    def test_load_version_mismatch(self, tmp_path):
        """Version mismatch returns None."""
        checkpoint = {"version": 99, "team_name": "test"}
        cp_file = tmp_path / "checkpoint.json"
        cp_file.write_text(json.dumps(checkpoint), encoding="utf-8")

        result = TeamMonitorHandler.load_task_checkpoint(str(tmp_path))
        assert result is None

    def test_load_empty_file(self, tmp_path):
        """Empty file returns None."""
        cp_file = tmp_path / "checkpoint.json"
        cp_file.write_text("", encoding="utf-8")

        result = TeamMonitorHandler.load_task_checkpoint(str(tmp_path))
        assert result is None

    def test_load_nonexistent_directory(self):
        """Non-existent directory returns None."""
        result = TeamMonitorHandler.load_task_checkpoint("/nonexistent/path/that/does/not/exist")
        assert result is None


# ===========================================================================
# Tests: Checkpoint save (atomic write)
# ===========================================================================

def _make_handler(monitor=None, session_id="sess-test"):
    """Create a TeamMonitorHandler bypassing __init__ with required attributes."""
    handler = TeamMonitorHandler.__new__(TeamMonitorHandler)
    handler._monitor = monitor
    handler._session_id = session_id
    handler._running = True
    handler._last_checkpoint_time = 0.0  # debounce attribute added by python-dev
    return handler


class TestCheckpointSave:
    """Test _save_task_checkpoint atomic write behavior."""

    @pytest.mark.asyncio
    async def test_save_creates_checkpoint_file(self, tmp_path):
        """Checkpoint save creates a valid JSON file."""
        mock_monitor = MagicMock()
        mock_monitor.team_name = "test-team"

        mock_member = MagicMock()
        mock_member.member_name = "member-1"
        mock_member.display_name = "Member 1"
        mock_member.desc = "Test member"
        mock_member.role = "teammate"
        mock_member.status = "active"

        mock_task = MagicMock()
        mock_task.task_id = "task-1"
        mock_task.title = "Test Task"
        mock_task.content = "Task content"
        mock_task.status = "in_progress"
        mock_task.assignee = "member-1"
        mock_task.depends_on = []
        mock_task.blocked_by = []

        handler = _make_handler(monitor=mock_monitor, session_id="sess-123")
        mock_monitor.get_members = AsyncMock(return_value=[mock_member])
        mock_monitor.get_tasks = AsyncMock(return_value=[mock_task])
        handler._resolve_team_workspace_dir = MagicMock(return_value=str(tmp_path))

        # set_session_id is imported inside the function body, so mock at source
        with patch("openjiuwen.agent_teams.context.set_session_id", return_value="token"):
            with patch("openjiuwen.agent_teams.context.reset_session_id"):
                await handler._save_task_checkpoint()

        cp_file = tmp_path / "checkpoint.json"
        assert cp_file.exists()
        data = json.loads(cp_file.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["team_name"] == "test-team"
        assert data["session_id"] == "sess-123"
        assert len(data["members"]) == 1
        assert data["members"][0]["member_name"] == "member-1"
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["task_id"] == "task-1"

    @pytest.mark.asyncio
    async def test_save_atomic_write_no_tmp_leftover(self, tmp_path):
        """Atomic write leaves no .tmp file behind."""
        mock_monitor = MagicMock()
        mock_monitor.team_name = "test-team"
        mock_monitor.get_members = AsyncMock(return_value=[])
        mock_monitor.get_tasks = AsyncMock(return_value=[])

        handler = _make_handler(monitor=mock_monitor, session_id="sess-456")
        handler._resolve_team_workspace_dir = MagicMock(return_value=str(tmp_path))

        with patch("openjiuwen.agent_teams.context.set_session_id", return_value="token"):
            with patch("openjiuwen.agent_teams.context.reset_session_id"):
                await handler._save_task_checkpoint()

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    @pytest.mark.asyncio
    async def test_save_handles_monitor_none(self):
        """Save gracefully handles None monitor."""
        handler = _make_handler(monitor=None, session_id="sess-789")
        # Should not raise
        await handler._save_task_checkpoint()

    @pytest.mark.asyncio
    async def test_save_handles_workspace_none(self):
        """Save gracefully handles None workspace directory."""
        mock_monitor = MagicMock()
        mock_monitor.team_name = "test-team"
        mock_monitor.get_members = AsyncMock(return_value=[])
        mock_monitor.get_tasks = AsyncMock(return_value=[])

        handler = _make_handler(monitor=mock_monitor, session_id="sess-000")
        handler._resolve_team_workspace_dir = MagicMock(return_value=None)

        with patch("openjiuwen.agent_teams.context.set_session_id", return_value="token"):
            with patch("openjiuwen.agent_teams.context.reset_session_id"):
                await handler._save_task_checkpoint()


# ===========================================================================
# Tests: Message normalization
# ===========================================================================

class TestMessageNormalization:
    """Test message protocol and content normalization."""

    def test_normalize_protocol_plain(self):
        """Plain protocol is normalized correctly."""
        assert TeamMonitorHandler._normalize_message_protocol("plain") == "plain"

    def test_normalize_protocol_none(self):
        """None protocol defaults to 'plain'."""
        assert TeamMonitorHandler._normalize_message_protocol(None) == "plain"

    def test_normalize_protocol_uppercase(self):
        """Uppercase protocol is lowercased."""
        assert TeamMonitorHandler._normalize_message_protocol("JSON") == "json"

    def test_normalize_protocol_whitespace(self):
        """Whitespace is stripped."""
        assert TeamMonitorHandler._normalize_message_protocol("  plain  ") == "plain"

    def test_normalize_protocol_empty(self):
        """Empty string defaults to 'plain'."""
        assert TeamMonitorHandler._normalize_message_protocol("") == "plain"

    def test_normalize_content_plain(self):
        """Plain content is returned as-is."""
        content = "Hello, world!"
        assert TeamMonitorHandler._normalize_message_content(content, "plain") == content

    def test_normalize_content_json_valid(self):
        """Valid JSON content is re-serialized with ensure_ascii=False."""
        content = '{"key": "value"}'
        result = TeamMonitorHandler._normalize_message_content(content, "json")
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_normalize_content_json_invalid(self):
        """Invalid JSON content is returned as-is."""
        content = "not json at all"
        result = TeamMonitorHandler._normalize_message_content(content, "json")
        assert result == content

    def test_normalize_content_json_empty(self):
        """Empty JSON content is returned as-is."""
        content = ""
        result = TeamMonitorHandler._normalize_message_content(content, "json")
        assert result == ""

    def test_normalize_content_json_unicode(self):
        """JSON with unicode is preserved correctly."""
        content = '{"name": "测试"}'
        result = TeamMonitorHandler._normalize_message_content(content, "json")
        assert "测试" in result


# ===========================================================================
# Tests: Task event status mapping
# ===========================================================================

class TestTaskEventStatusMapping:
    """Test the _TASK_EVENT_STATUS mapping completeness."""

    def test_status_mapping_is_dict(self):
        """_TASK_EVENT_STATUS is a non-empty dict."""
        assert isinstance(_TASK_EVENT_STATUS, dict)
        assert len(_TASK_EVENT_STATUS) > 0

    def test_all_values_are_strings(self):
        """All status values are non-empty strings."""
        for event_type, status in _TASK_EVENT_STATUS.items():
            assert isinstance(status, str)
            assert len(status) > 0

    def test_known_statuses(self):
        """Key event types map to expected statuses."""
        from openjiuwen.agent_teams.monitor.models import MonitorEventType
        assert _TASK_EVENT_STATUS[MonitorEventType.TASK_CREATED] == "pending"
        assert _TASK_EVENT_STATUS[MonitorEventType.TASK_COMPLETED] == "completed"
        assert _TASK_EVENT_STATUS[MonitorEventType.TASK_CANCELLED] == "cancelled"
        assert _TASK_EVENT_STATUS[MonitorEventType.TASK_CLAIMED] == "in_progress"
