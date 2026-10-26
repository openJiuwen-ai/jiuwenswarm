"""Unit tests for copy_session_state."""
from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestCopySessionStateNoSource:
    """When source has no DeepAgentState, copy_session_state returns False."""

    @pytest.mark.asyncio
    async def test_no_source_state_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path / "sessions",
        )
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_workspace_dir",
            lambda: tmp_path / "workspace",
        )
        mock_session = MagicMock()
        mock_session.pre_run = AsyncMock(return_value=mock_session)
        mock_session.post_run = AsyncMock(return_value=mock_session)
        mock_session.get_state.return_value = None

        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            return_value=mock_session,
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import copy_session_state

            result = await copy_session_state(
                source_session_id="source_empty",
                target_session_id="target_empty",
                card=MagicMock(),
            )
        assert result is False


class TestCopySessionStateTransform:
    """Verify state transformation rules during copy."""

    @pytest.mark.asyncio
    async def test_iteration_reset_to_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path / "sessions",
        )
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_workspace_dir",
            lambda: tmp_path / "workspace",
        )

        source_state = {
            "iteration": 5,
            "task_plan": {"goal": "Test", "tasks": [], "current_task_id": None},
            "stop_condition_state": {"should_continue": True},
            "pending_follow_ups": ["follow_up_1"],
            "plan_mode": {"mode": "normal", "pre_plan_mode": "normal", "plan_slug": None},
        }

        source_mock = MagicMock()
        source_mock.pre_run = AsyncMock(return_value=source_mock)
        source_mock.post_run = AsyncMock(return_value=source_mock)
        source_mock.get_state.return_value = source_state

        target_mock = MagicMock()
        target_mock.pre_run = AsyncMock(return_value=target_mock)
        target_mock.post_run = AsyncMock(return_value=target_mock)

        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            side_effect=[source_mock, target_mock],
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import copy_session_state

            result = await copy_session_state(
                source_session_id="source_1",
                target_session_id="target_1",
                card=MagicMock(),
            )

        assert result is True
        update_call = target_mock.update_state.call_args
        written_state = update_call[0][0]["deepagent"]
        assert written_state["iteration"] == 0
        assert written_state["stop_condition_state"] is None
        assert written_state["pending_follow_ups"] == []
        assert written_state["task_plan"] == source_state["task_plan"]
        assert written_state["plan_mode"]["mode"] == "normal"

    @pytest.mark.asyncio
    async def test_plan_slug_generates_new_and_copies_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path / "sessions",
        )
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_workspace_dir",
            lambda: tmp_path / "workspace",
        )

        plans_dir = tmp_path / "workspace" / ".plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        old_plan = plans_dir / "old-slug.md"
        old_plan.write_text("# Plan content\n- Task 1\n- Task 2", encoding="utf-8")

        source_state = {
            "iteration": 3,
            "task_plan": None,
            "stop_condition_state": None,
            "pending_follow_ups": [],
            "plan_mode": {"mode": "plan", "pre_plan_mode": "normal", "plan_slug": "old-slug"},
        }

        source_mock = MagicMock()
        source_mock.pre_run = AsyncMock(return_value=source_mock)
        source_mock.post_run = AsyncMock(return_value=source_mock)
        source_mock.get_state.return_value = source_state

        target_mock = MagicMock()
        target_mock.pre_run = AsyncMock(return_value=target_mock)
        target_mock.post_run = AsyncMock(return_value=target_mock)

        new_slug = "new-fork-slug"
        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            side_effect=[source_mock, target_mock],
        ), patch(
            "openjiuwen.harness.tools.agent_mode_tools.get_or_create_plan_slug",
            return_value=new_slug,
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import copy_session_state

            result = await copy_session_state(
                source_session_id="source_plan",
                target_session_id="target_plan",
                card=MagicMock(),
            )

        assert result is True
        new_plan = plans_dir / f"{new_slug}.md"
        assert new_plan.exists()
        assert new_plan.read_text() == old_plan.read_text()

        update_call = target_mock.update_state.call_args
        written_state = update_call[0][0]["deepagent"]
        assert written_state["plan_mode"]["plan_slug"] == new_slug


class TestCopySessionStateDeepAgent:
    """Test with deep_agent parameter (flush source state)."""

    @pytest.mark.asyncio
    async def test_deep_agent_flush_before_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path / "sessions",
        )
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_workspace_dir",
            lambda: tmp_path / "workspace",
        )

        source_state = {
            "iteration": 10,
            "task_plan": {"goal": "Build feature", "tasks": [{"id": "t1", "content": "Write code"}]},
            "stop_condition_state": {"iteration_count": 10},
            "pending_follow_ups": ["q1", "q2"],
            "plan_mode": {"mode": "normal", "pre_plan_mode": "normal", "plan_slug": None},
        }

        mock_deep_agent = MagicMock()
        mock_deep_agent.card = MagicMock()

        source_mock = MagicMock()
        source_mock.pre_run = AsyncMock(return_value=source_mock)
        source_mock.post_run = AsyncMock(return_value=source_mock)
        source_mock.get_state.return_value = source_state

        target_mock = MagicMock()
        target_mock.pre_run = AsyncMock(return_value=target_mock)
        target_mock.post_run = AsyncMock(return_value=target_mock)

        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            side_effect=[source_mock, target_mock],
        ), patch(
            "jiuwenswarm.agents.harness.common.session_ops_service._flush_source_state",
        ) as mock_flush:
            from jiuwenswarm.agents.harness.common.session_ops_service import copy_session_state

            result = await copy_session_state(
                source_session_id="source_da",
                target_session_id="target_da",
                card=mock_deep_agent.card,
                deep_agent=mock_deep_agent,
            )

        assert result is True
        mock_flush.assert_called_once_with(mock_deep_agent, "source_da")

        update_call = target_mock.update_state.call_args
        written_state = update_call[0][0]["deepagent"]
        assert written_state["iteration"] == 0
        assert written_state["stop_condition_state"] is None
        assert written_state["pending_follow_ups"] == []
        assert written_state["task_plan"]["goal"] == "Build feature"