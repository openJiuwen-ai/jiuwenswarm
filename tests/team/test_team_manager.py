# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for TeamManager resume and checkpoint logic.

Covers:
- resume_team_from_checkpoint: various scenarios (valid, missing, invalid)
- _find_latest_checkpoint: robustness (empty dir, multiple candidates, errors)
- build_session_scoped_team_name: naming conventions
- _normalize_team_identity_fields: display_name/name normalization
- Session lifecycle helpers (active/pending/waiters tracking)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# We import TeamManager carefully to avoid triggering heavy side effects
# from module-level imports. The class itself is importable without
# starting services.
from jiuwenswarm.agents.harness.team.team_manager import TeamManager


# ===========================================================================
# Tests: build_session_scoped_team_name
# ===========================================================================

class TestBuildSessionScopedTeamName:
    """Test the static method for building session-scoped team names."""

    def test_basic_name_and_session(self):
        """Basic team name gets session suffix appended."""
        result = TeamManager.build_session_scoped_team_name("my-team", "sess-123")
        assert result == "my-team_sess-123"

    def test_empty_team_name_defaults_to_team(self):
        """Empty team name defaults to 'team'."""
        result = TeamManager.build_session_scoped_team_name("", "sess-123")
        assert result == "team_sess-123"

    def test_none_team_name_defaults_to_team(self):
        """None team name defaults to 'team'."""
        result = TeamManager.build_session_scoped_team_name(None, "sess-123")
        assert result == "team_sess-123"

    def test_empty_session_id_returns_base_name(self):
        """Empty session ID returns base name without suffix."""
        result = TeamManager.build_session_scoped_team_name("my-team", "")
        assert result == "my-team"

    def test_none_session_id_returns_base_name(self):
        """None session ID returns base name without suffix."""
        result = TeamManager.build_session_scoped_team_name("my-team", None)
        assert result == "my-team"

    def test_already_has_suffix_no_duplication(self):
        """If name already ends with session suffix, no duplication."""
        result = TeamManager.build_session_scoped_team_name("my-team_sess-123", "sess-123")
        assert result == "my-team_sess-123"

    def test_special_chars_in_session_sanitized(self):
        """Special characters in session ID are sanitized."""
        result = TeamManager.build_session_scoped_team_name("team", "sess/123:abc")
        # Special chars replaced with underscore
        assert "/" not in result
        assert ":" not in result

    def test_whitespace_only_session(self):
        """Whitespace-only session ID returns base name."""
        result = TeamManager.build_session_scoped_team_name("team", "   ")
        assert result == "team"


# ===========================================================================
# Tests: _normalize_team_identity_fields
# ===========================================================================

class TestNormalizeTeamIdentityFields:
    """Test display_name/name normalization for leader and members."""

    def test_leader_display_name_to_name(self):
        """Leader display_name is copied to name when name is empty."""
        team_cfg = {
            "leader": {"display_name": "My Leader", "name": ""},
            "predefined_members": [],
        }
        result = TeamManager._normalize_team_identity_fields(team_cfg)
        assert result["leader"]["name"] == "My Leader"

    def test_leader_name_to_display_name(self):
        """Leader name is copied to display_name when display_name is empty."""
        team_cfg = {
            "leader": {"display_name": "", "name": "leader-1"},
            "predefined_members": [],
        }
        result = TeamManager._normalize_team_identity_fields(team_cfg)
        assert result["leader"]["display_name"] == "leader-1"

    def test_leader_both_set_unchanged(self):
        """When both name and display_name are set, neither is overwritten."""
        team_cfg = {
            "leader": {"display_name": "Display", "name": "Name"},
            "predefined_members": [],
        }
        result = TeamManager._normalize_team_identity_fields(team_cfg)
        assert result["leader"]["display_name"] == "Display"
        assert result["leader"]["name"] == "Name"

    def test_member_display_name_to_name(self):
        """Member display_name is copied to name when name is empty."""
        team_cfg = {
            "leader": {},
            "predefined_members": [
                {"display_name": "Dev Member", "name": ""},
            ],
        }
        result = TeamManager._normalize_team_identity_fields(team_cfg)
        assert result["predefined_members"][0]["name"] == "Dev Member"

    def test_member_name_to_display_name(self):
        """Member name is copied to display_name when display_name is empty."""
        team_cfg = {
            "leader": {},
            "predefined_members": [
                {"display_name": "", "name": "dev-1"},
            ],
        }
        result = TeamManager._normalize_team_identity_fields(team_cfg)
        assert result["predefined_members"][0]["display_name"] == "dev-1"

    def test_non_dict_members_skipped(self):
        """Non-dict entries in predefined_members are skipped gracefully."""
        team_cfg = {
            "leader": {},
            "predefined_members": ["not_a_dict", 42, None],
        }
        result = TeamManager._normalize_team_identity_fields(team_cfg)
        # Should not raise, non-dict entries preserved
        assert len(result["predefined_members"]) == 3

    def test_deep_copy_no_mutation(self):
        """Original config is not mutated."""
        team_cfg = {
            "leader": {"display_name": "Leader", "name": ""},
            "predefined_members": [],
        }
        original_leader_name = team_cfg["leader"]["name"]
        TeamManager._normalize_team_identity_fields(team_cfg)
        assert team_cfg["leader"]["name"] == original_leader_name

    def test_empty_config(self):
        """Empty config is handled gracefully."""
        result = TeamManager._normalize_team_identity_fields({})
        assert isinstance(result, dict)


# ===========================================================================
# Tests: _find_latest_checkpoint
# ===========================================================================

class TestFindLatestCheckpoint:
    """Test _find_latest_checkpoint search logic."""

    def test_no_agent_teams_dir(self, tmp_path):
        """Returns None when .agent_teams directory doesn't exist."""
        manager = TeamManager()
        # get_user_workspace_dir is imported at module level via
        # ``from jiuwenswarm.common.utils import get_user_workspace_dir``,
        # so we must patch the name in the *importing* module's namespace.
        with patch(
            "jiuwenswarm.agents.harness.team.team_manager.get_user_workspace_dir",
            return_value=tmp_path,
        ):
            result = manager._find_latest_checkpoint()
            assert result is None

    def test_empty_agent_teams_dir(self, tmp_path):
        """Returns None when .agent_teams exists but has no checkpoints."""
        agent_teams = tmp_path / ".agent_teams"
        agent_teams.mkdir()

        manager = TeamManager()
        with patch(
            "jiuwenswarm.agents.harness.team.team_manager.get_user_workspace_dir",
            return_value=tmp_path,
        ):
            result = manager._find_latest_checkpoint()
            assert result is None

    def test_single_checkpoint_found(self, tmp_path):
        """Single checkpoint file is found."""
        team_ws = tmp_path / ".agent_teams" / "team1" / "team-workspace"
        team_ws.mkdir(parents=True)
        cp_file = team_ws / "checkpoint.json"
        cp_file.write_text('{"version": 1}', encoding="utf-8")

        manager = TeamManager()
        # get_user_workspace_dir is now imported at module level
        with patch(
            "jiuwenswarm.agents.harness.team.team_manager.get_user_workspace_dir",
            return_value=tmp_path,
        ):
            result = manager._find_latest_checkpoint()
            assert result is not None
            assert result.name == "checkpoint.json"

    def test_multiple_checkpoints_returns_latest(self, tmp_path):
        """Multiple checkpoints → most recently modified one is returned."""
        # Create two team workspaces with checkpoints
        for i, team_name in enumerate(["team-old", "team-new"]):
            ws = tmp_path / ".agent_teams" / team_name / "team-workspace"
            ws.mkdir(parents=True)
            cp = ws / "checkpoint.json"
            cp.write_text(f'{{"version": 1, "team": "{team_name}"}}', encoding="utf-8")
            # Set different modification times
            mtime = 1000000 + i * 1000
            os.utime(str(cp), (mtime, mtime))

        manager = TeamManager()
        with patch(
            "jiuwenswarm.agents.harness.team.team_manager.get_user_workspace_dir",
            return_value=tmp_path,
        ):
            result = manager._find_latest_checkpoint()
            assert result is not None
            data = json.loads(result.read_text(encoding="utf-8"))
            assert data["team"] == "team-new"

    def test_exception_returns_none(self, tmp_path):
        """Exception during search returns None (graceful degradation)."""
        manager = TeamManager()
        with patch(
            "jiuwenswarm.agents.harness.team.team_manager.get_user_workspace_dir",
            side_effect=RuntimeError("disk error"),
        ):
            result = manager._find_latest_checkpoint()
            assert result is None


# ===========================================================================
# Tests: resume_team_from_checkpoint
# ===========================================================================

class TestResumeTeamFromCheckpoint:
    """Test resume_team_from_checkpoint various scenarios."""

    @pytest.mark.asyncio
    async def test_resume_no_checkpoint_path(self):
        """Resume with no checkpoint path and no auto-detected checkpoint."""
        manager = TeamManager()
        mock_agent = MagicMock()

        with patch.object(manager, "_find_latest_checkpoint", return_value=None):
            result = await manager.resume_team_from_checkpoint(
                "sess-1", mock_agent
            )
            assert result["resumed"] is False
            assert result["reason"] == "no_checkpoint"

    @pytest.mark.asyncio
    async def test_resume_nonexistent_path(self):
        """Resume with explicit path that doesn't exist."""
        manager = TeamManager()
        mock_agent = MagicMock()

        result = await manager.resume_team_from_checkpoint(
            "sess-1", mock_agent,
            checkpoint_path="/nonexistent/checkpoint.json",
        )
        assert result["resumed"] is False
        assert result["reason"] == "no_checkpoint"

    @pytest.mark.asyncio
    async def test_resume_invalid_checkpoint_content(self, tmp_path):
        """Resume with checkpoint file containing invalid JSON."""
        cp_file = tmp_path / "checkpoint.json"
        cp_file.write_text("invalid json", encoding="utf-8")

        manager = TeamManager()
        mock_agent = MagicMock()

        result = await manager.resume_team_from_checkpoint(
            "sess-1", mock_agent,
            checkpoint_path=str(cp_file),
        )
        assert result["resumed"] is False
        assert result["reason"] == "invalid_checkpoint"

    @pytest.mark.asyncio
    async def test_resume_valid_checkpoint(self, tmp_path):
        """Resume with valid checkpoint creates team and returns summary."""
        checkpoint = {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "team_name": "test-team",
            "session_id": "old-sess",
            "members": [
                {"member_name": "m1", "role": "teammate", "status": "active"},
            ],
            "tasks": [
                {"task_id": "t1", "status": "completed", "title": "Done"},
                {"task_id": "t2", "status": "in_progress", "title": "WIP"},
                {"task_id": "t3", "status": "pending", "title": "Todo"},
            ],
        }
        cp_file = tmp_path / "checkpoint.json"
        cp_file.write_text(json.dumps(checkpoint), encoding="utf-8")

        manager = TeamManager()
        mock_agent = MagicMock()

        # Mock create_team to avoid actual team creation
        mock_team_agent = MagicMock()
        manager.create_team = AsyncMock(return_value=mock_team_agent)

        result = await manager.resume_team_from_checkpoint(
            "sess-new", mock_agent,
            checkpoint_path=str(cp_file),
        )
        assert result["resumed"] is True
        assert result["team_name"] == "test-team"
        assert result["members_in_checkpoint"] == 1
        assert result["tasks_restored"] == 2  # in_progress + pending
        assert result["tasks_completed"] == 1  # completed
        assert "checkpoint_age_seconds" in result

    @pytest.mark.asyncio
    async def test_resume_checkpoint_with_model_override(self, tmp_path):
        """Model override is included in result."""
        checkpoint = {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "team_name": "test-team",
            "session_id": "old-sess",
            "members": [],
            "tasks": [],
        }
        cp_file = tmp_path / "checkpoint.json"
        cp_file.write_text(json.dumps(checkpoint), encoding="utf-8")

        manager = TeamManager()
        mock_agent = MagicMock()
        manager.create_team = AsyncMock(return_value=MagicMock())

        result = await manager.resume_team_from_checkpoint(
            "sess-new", mock_agent,
            checkpoint_path=str(cp_file),
            model_override="qwen3.8-max",
        )
        assert result["model_override"] == "qwen3.8-max"


# ===========================================================================
# Tests: Session lifecycle helpers
# ===========================================================================

class TestSessionLifecycleHelpers:
    """Test active/pending/waiter tracking methods."""

    def test_has_stream_task(self):
        """has_stream_task returns correct state."""
        manager = TeamManager()
        assert not manager.has_stream_task("s1")
        manager._stream_tasks["s1"] = MagicMock()
        assert manager.has_stream_task("s1")

    def test_pop_stream_task(self):
        """pop_stream_task removes and returns the task."""
        manager = TeamManager()
        task = MagicMock()
        manager._stream_tasks["s1"] = task
        assert manager.pop_stream_task("s1") is task
        assert not manager.has_stream_task("s1")
        assert manager.pop_stream_task("s1") is None

    def test_is_session_initialized(self):
        """is_session_initialized tracks initialized sessions."""
        manager = TeamManager()
        assert not manager.is_session_initialized("s1")
        manager._initialized_sessions.add("s1")
        assert manager.is_session_initialized("s1")

    def test_clear_session_initialized(self):
        """clear_session_initialized removes the marker."""
        manager = TeamManager()
        manager._initialized_sessions.add("s1")
        manager.clear_session_initialized("s1")
        assert not manager.is_session_initialized("s1")

    def test_waiters_add_remove(self):
        """Waiters can be added and removed."""
        import asyncio
        manager = TeamManager()
        q = asyncio.Queue()
        manager.add_waiter("s1", "req-1", q)
        assert manager.has_waiters("s1")
        manager.remove_waiter("s1", "req-1")
        assert not manager.has_waiters("s1")

    def test_waiters_multiple_requests(self):
        """Multiple waiters for same session."""
        import asyncio
        manager = TeamManager()
        q1, q2 = asyncio.Queue(), asyncio.Queue()
        manager.add_waiter("s1", "req-1", q1)
        manager.add_waiter("s1", "req-2", q2)
        assert manager.has_waiters("s1")
        manager.remove_waiter("s1", "req-1")
        assert manager.has_waiters("s1")  # req-2 still there
        manager.remove_waiter("s1", "req-2")
        assert not manager.has_waiters("s1")

    async def test_broadcast_event(self):
        """broadcast_event puts event into all waiter queues."""
        import asyncio
        manager = TeamManager()
        q1, q2 = asyncio.Queue(), asyncio.Queue()
        manager.add_waiter("s1", "req-1", q1)
        manager.add_waiter("s1", "req-2", q2)
        event = {"type": "test_event"}
        await manager.broadcast_event("s1", event)
        assert not q1.empty()
        assert not q2.empty()

    def test_commit_runtime_ready(self):
        """commit_runtime_ready sets active and clears pending."""
        manager = TeamManager()
        manager._pending_team_names["s1"] = "team-1"
        manager.commit_runtime_ready("s1", "team-1")
        assert manager.is_runtime_active("s1")
        assert not manager.is_runtime_pending("s1")
        assert manager.get_active_team_name("s1") == "team-1"
        assert manager.is_session_initialized("s1")

    def test_clear_active_runtime(self):
        """clear_active_runtime removes the active marker."""
        manager = TeamManager()
        manager._active_team_names["s1"] = "team-1"
        manager.clear_active_runtime("s1")
        assert not manager.is_runtime_active("s1")

    def test_clear_pending_runtime(self):
        """clear_pending_runtime removes the pending marker."""
        manager = TeamManager()
        manager._pending_team_names["s1"] = "team-1"
        manager.clear_pending_runtime("s1")
        assert not manager.is_runtime_pending("s1")


    def test_get_runtime_team_snapshot(self):
        """get_runtime_team_snapshot returns active and pending entries."""
        manager = TeamManager()
        manager._active_team_names["s1"] = "team-a"
        manager._pending_team_names["s2"] = "team-b"
        snapshot = manager.get_runtime_team_snapshot()
        assert snapshot["s1"]["state"] == "active"
        assert snapshot["s1"]["team_name"] == "team-a"
        assert snapshot["s2"]["state"] == "pending"
        assert snapshot["s2"]["team_name"] == "team-b"

    def test_cron_completion_lifecycle(self):
        """Cron completion state can be set, retrieved, and popped."""
        manager = TeamManager()
        assert manager.get_cron_completion("s1") is None
        state = {"done": True}
        result = manager.setdefault_cron_completion("s1", state)
        assert result is state
        assert manager.get_cron_completion("s1") is state
        popped = manager.pop_cron_completion("s1")
        assert popped is state
        assert manager.get_cron_completion("s1") is None

    def test_remove_waiter_nonexistent_session(self):
        """Removing waiter from nonexistent session is a no-op."""
        manager = TeamManager()
        manager.remove_waiter("nonexistent", "req-1")  # Should not raise

    def test_broadcast_event_no_waiters(self):
        """Broadcasting to session with no waiters is a no-op."""
        manager = TeamManager()
        manager.broadcast_event("s1", {"type": "test"})  # Should not raise
