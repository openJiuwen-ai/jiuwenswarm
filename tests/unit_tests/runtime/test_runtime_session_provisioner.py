# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_provisioner import SessionDeleteResult


@dataclass
class _DeleteState:
    events: list[tuple[Any, ...]] = field(default_factory=list)
    failures: dict[str, BaseException] = field(default_factory=dict)
    metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "mode": "agent.work.normal",
            "channel_id": "metadata-channel",
        }
    )
    evict_result: bool = True
    team_delete_result: bool = True

    def hit(self, stage: str, *values: Any) -> None:
        self.events.append((stage, *values))
        failure = self.failures.get(stage)
        if failure is not None:
            raise failure


class _AgentManager:
    def __init__(self, state: _DeleteState) -> None:
        self._state = state

    async def release_subagent_runtime_for_session(
        self,
        *,
        channel_id: str,
        session_id: str,
        reason: str,
    ) -> None:
        self._state.hit(
            "release_subagent",
            channel_id,
            session_id,
            reason,
        )


class _PlanController:
    def __init__(self, state: _DeleteState) -> None:
        self._state = state
        self.active_sessions: set[str] = set()
        self.exited_sessions: set[str] = set()

    def reset_session(self, session_id: str) -> None:
        self._state.hit("plan.reset", session_id)
        self.active_sessions.discard(session_id)
        self.exited_sessions.discard(session_id)


class _DeleteLifecycle:
    is_available = True

    def __init__(self, state: _DeleteState) -> None:
        self._state = state

    async def start(self) -> None:
        self._state.hit("lifecycle.start")
        self.is_available = True

    async def begin_session_delete(self, session_id: str) -> None:
        self._state.hit("lifecycle.begin", session_id)

    async def abort_session_delete(
        self,
        session_id: str,
        *,
        channel_id: str = "",
    ) -> None:
        self._state.hit("lifecycle.abort", session_id, channel_id)

    async def commit_session_delete(self, session_id: str) -> None:
        self._state.hit("lifecycle.commit", session_id)


class _TeamManager:
    def __init__(self, state: _DeleteState) -> None:
        self._state = state

    async def delete_session_runtime(
        self,
        session_id: str,
        reason: str = "",
    ) -> bool:
        self._state.hit("team.delete", session_id, reason)
        return self._state.team_delete_result


class _TeamBindingStore:
    def __init__(self, state: _DeleteState) -> None:
        self._state = state

    def unbind_session(
        self,
        *,
        team_name: str | None,
        session_id: str,
    ) -> None:
        self._state.hit("team.unbind", team_name, session_id)


@dataclass
class _DeleteEnvironment:
    root: Path
    state: _DeleteState
    plan: _PlanController
    lifecycle: _DeleteLifecycle
    runtime: AgentRuntime

    def create_session(self, session_id: str) -> Path:
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True)
        (session_dir / "history.jsonl").write_text("{}\n", encoding="utf-8")
        self.plan.active_sessions.add(session_id)
        self.plan.exited_sessions.add(session_id)
        return session_dir


@pytest.fixture
def delete_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> _DeleteEnvironment:
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.agents.harness import team as team_package
    from jiuwenswarm.common import utils
    from jiuwenswarm.observability import session_delete as trajectory_delete
    from jiuwenswarm.runtime import session_provisioner as provisioner_module
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime import team_binding_store
    from jiuwenswarm.server.runtime.session import session_metadata
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    root = tmp_path / "sessions"
    root.mkdir()
    state = _DeleteState()
    manager = _AgentManager(state)
    plan = _PlanController(state)
    lifecycle = _DeleteLifecycle(state)
    team_manager = _TeamManager(state)
    binding_store = _TeamBindingStore(state)

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=initialize,
        plan_controller=plan,
        session_delete_lifecycle=lifecycle,
    )

    async def cleanup_session(
        *,
        channel_id: str,
        session_id: str,
        reset_plan_state: bool = True,
    ) -> bool:
        state.hit(
            "cleanup",
            channel_id,
            session_id,
            reset_plan_state,
        )
        return True

    runtime.cleanup_session = cleanup_session  # type: ignore[method-assign]

    async def ensure_persistent_checkpointer() -> None:
        state.hit("checkpointer")

    def get_session_metadata(session_id: str) -> dict[str, Any]:
        state.hit("metadata", session_id)
        return dict(state.metadata)

    def mark_session_deleted(
        *,
        session_id: str,
        channel_id: str,
        is_team: bool,
    ) -> None:
        state.hit("kvc.mark", session_id, channel_id, is_team)

    async def evict_plan_session(
        *,
        session_id: str,
    ) -> bool:
        state.hit("kvc.evict", session_id)
        return state.evict_result

    def restore_session_after_failed_delete(session_id: str) -> None:
        state.hit("kvc.restore", session_id)

    def begin_trajectory_session_delete(session_id: str) -> None:
        state.hit("trajectory.begin", session_id)

    def abort_trajectory_session_delete(session_id: str) -> None:
        state.hit("trajectory.abort", session_id)

    def commit_trajectory_session_delete(session_id: str) -> None:
        state.hit("trajectory.commit", session_id)

    async def release_runner(session_id: str) -> None:
        state.hit("runner.release", session_id)

    def remove_session_metadata_cache(session_id: str) -> None:
        state.hit("metadata.remove", session_id)

    def get_team_manager(channel_id: str) -> _TeamManager:
        state.hit("team.manager", channel_id)
        return team_manager

    real_rmtree = shutil.rmtree

    def remove_session_dir(path: str | Path) -> None:
        resolved = Path(path)
        state.hit("filesystem.remove", resolved)
        real_rmtree(resolved)

    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(
        interface_deep,
        "ensure_persistent_checkpointer",
        ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        get_session_metadata,
    )
    monkeypatch.setattr(
        session_metadata,
        "remove_session_metadata_cache",
        remove_session_metadata_cache,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "mark_session_deleted",
        mark_session_deleted,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "evict_plan_session",
        evict_plan_session,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "restore_session_after_failed_delete",
        restore_session_after_failed_delete,
    )
    monkeypatch.setattr(
        trajectory_delete,
        "begin_trajectory_session_delete",
        begin_trajectory_session_delete,
    )
    monkeypatch.setattr(
        trajectory_delete,
        "abort_trajectory_session_delete",
        abort_trajectory_session_delete,
    )
    monkeypatch.setattr(
        trajectory_delete,
        "commit_trajectory_session_delete",
        commit_trajectory_session_delete,
    )
    monkeypatch.setattr(Runner, "release", release_runner)
    monkeypatch.setattr(team_package, "get_team_manager", get_team_manager)
    monkeypatch.setattr(
        team_binding_store,
        "get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(provisioner_module.shutil, "rmtree", remove_session_dir)

    return _DeleteEnvironment(
        root=root,
        state=state,
        plan=plan,
        lifecycle=lifecycle,
        runtime=runtime,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "path_kind", "checkpoint_failure", "expected"),
    [
        (
            "  ",
            None,
            False,
            SessionDeleteResult.failure(
                "",
                code="BAD_REQUEST",
                message="session_id is required",
            ),
        ),
        (
            "../escape",
            None,
            False,
            SessionDeleteResult.failure(
                "../escape",
                code="BAD_REQUEST",
                message="invalid session_id",
            ),
        ),
        (
            "missing-session",
            None,
            False,
            SessionDeleteResult.failure(
                "missing-session",
                code="NOT_FOUND",
                message="session not found",
            ),
        ),
        (
            "file-session",
            "file",
            False,
            SessionDeleteResult.failure(
                "file-session",
                code="BAD_REQUEST",
                message="session is not a directory",
            ),
        ),
        (
            "checkpoint-session",
            "directory",
            True,
            SessionDeleteResult.failure(
                "checkpoint-session",
                code="CHECKPOINT_UNAVAILABLE",
                message="persistent checkpointer is unavailable",
            ),
        ),
    ],
)
async def test_delete_session_validation_and_checkpointer_results_are_stable(
    delete_env: _DeleteEnvironment,
    session_id: str,
    path_kind: str | None,
    checkpoint_failure: bool,
    expected: SessionDeleteResult,
) -> None:
    target = session_id.strip()
    if path_kind == "file":
        (delete_env.root / target).write_text("not a directory", encoding="utf-8")
    elif path_kind == "directory":
        delete_env.create_session(target)
    if checkpoint_failure:
        delete_env.state.failures["checkpointer"] = RuntimeError("offline")

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result == expected
    assert delete_env.plan.active_sessions == (
        {target} if path_kind == "directory" else set()
    )
    assert delete_env.plan.exited_sessions == (
        {target} if path_kind == "directory" else set()
    )
    assert delete_env.state.events == (
        [("checkpointer",)] if checkpoint_failure else []
    )


@pytest.mark.asyncio
async def test_non_team_delete_preserves_order_and_uses_metadata_channel(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "agent-session"
    session_dir = delete_env.create_session(session_id)
    delete_env.state.metadata = {
        "mode": "agent.code.normal",
        "channel_id": "metadata-channel",
    }
    delete_env.state.evict_result = False

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=f"  {session_id}  ",
    )

    assert result == SessionDeleteResult(
        ok=True,
        session_id=session_id,
        channel_id="metadata-channel",
        is_team=False,
        team_name="",
    )
    assert delete_env.state.events == [
        ("checkpointer",),
        ("metadata", session_id),
        ("kvc.mark", session_id, "metadata-channel", False),
        ("trajectory.begin", session_id),
        ("lifecycle.begin", session_id),
        (
            "release_subagent",
            "metadata-channel",
            session_id,
            "session_deleted",
        ),
        ("cleanup", "metadata-channel", session_id, False),
        ("kvc.evict", session_id),
        ("runner.release", session_id),
        ("filesystem.remove", session_dir),
        ("trajectory.commit", session_id),
        ("lifecycle.commit", session_id),
        ("plan.reset", session_id),
        ("metadata.remove", session_id),
    ]
    assert not session_dir.exists()
    assert session_id not in delete_env.plan.active_sessions
    assert session_id not in delete_env.plan.exited_sessions


@pytest.mark.asyncio
async def test_team_delete_commits_plan_metadata_and_binding_only_after_success(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "team-session"
    session_dir = delete_env.create_session(session_id)
    delete_env.state.metadata = {
        "mode": "team.work.normal",
        "channel_id": "team-channel",
        "team_name": "research-team",
    }

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result == SessionDeleteResult(
        ok=True,
        session_id=session_id,
        channel_id="team-channel",
        is_team=True,
        team_name="research-team",
    )
    assert delete_env.state.events == [
        ("checkpointer",),
        ("metadata", session_id),
        ("kvc.mark", session_id, "team-channel", True),
        ("lifecycle.begin", session_id),
        ("team.manager", "team-channel"),
        ("team.delete", session_id, "session.delete: "),
        ("filesystem.remove", session_dir),
        ("lifecycle.commit", session_id),
        ("plan.reset", session_id),
        ("metadata.remove", session_id),
        ("team.unbind", "research-team", session_id),
    ]
    assert not session_dir.exists()
    assert session_id not in delete_env.plan.active_sessions
    assert session_id not in delete_env.plan.exited_sessions


@pytest.mark.asyncio
async def test_team_false_rolls_back_without_unbind_or_plan_commit(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "busy-team-session"
    session_dir = delete_env.create_session(session_id)
    delete_env.state.metadata = {
        "mode": "team.code.normal",
        "channel_id": "team-channel",
        "team_name": "busy-team",
    }
    delete_env.state.team_delete_result = False

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result == SessionDeleteResult.failure(
        session_id,
        code="DELETE_FAILED",
        message="session runtime cleanup failed",
    )
    assert delete_env.state.events == [
        ("checkpointer",),
        ("metadata", session_id),
        ("kvc.mark", session_id, "team-channel", True),
        ("lifecycle.begin", session_id),
        ("team.manager", "team-channel"),
        ("team.delete", session_id, "session.delete: "),
        ("lifecycle.abort", session_id, "team-channel"),
        ("kvc.restore", session_id),
    ]
    assert session_dir.exists()
    assert delete_env.plan.active_sessions == {session_id}
    assert delete_env.plan.exited_sessions == {session_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("start_fails", [False, True])
async def test_delete_preserves_best_effort_lifecycle_cold_start(
    delete_env: _DeleteEnvironment,
    start_fails: bool,
) -> None:
    session_id = "cold-lifecycle-session"
    delete_env.create_session(session_id)
    delete_env.lifecycle.is_available = False
    if start_fails:
        delete_env.state.failures["lifecycle.start"] = RuntimeError("not ready")

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result.ok is True
    assert delete_env.state.events[:5] == [
        ("checkpointer",),
        ("lifecycle.start",),
        ("metadata", session_id),
        ("kvc.mark", session_id, "metadata-channel", False),
        ("trajectory.begin", session_id),
    ]
    assert ("lifecycle.begin", session_id) in delete_env.state.events


@pytest.mark.asyncio
async def test_kvc_tombstone_failure_remains_best_effort(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "tombstone-failure-session"
    delete_env.create_session(session_id)
    delete_env.state.failures["kvc.mark"] = RuntimeError("mark failed")

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result.ok is True
    assert ("kvc.mark", session_id, "metadata-channel", False) in (
        delete_env.state.events
    )
    assert ("plan.reset", session_id) in delete_env.state.events


@pytest.mark.asyncio
async def test_kvc_restore_failure_preserves_primary_delete_failure(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "restore-failure-session"
    session_dir = delete_env.create_session(session_id)
    delete_env.state.failures["cleanup"] = RuntimeError("cleanup failed")
    delete_env.state.failures["kvc.restore"] = RuntimeError("restore failed")

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result == SessionDeleteResult.failure(
        session_id,
        code="DELETE_FAILED",
        message="session runtime cleanup failed",
    )
    assert delete_env.state.events[-3:] == [
        ("trajectory.abort", session_id),
        ("lifecycle.abort", session_id, "metadata-channel"),
        ("kvc.restore", session_id),
    ]
    assert session_dir.exists()
    assert delete_env.plan.active_sessions == {session_id}


@pytest.mark.asyncio
async def test_team_unbind_failure_does_not_flip_committed_delete(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "team-unbind-failure"
    session_dir = delete_env.create_session(session_id)
    delete_env.state.metadata = {
        "mode": "team.work.normal",
        "channel_id": "team-channel",
        "team_name": "research-team",
    }
    delete_env.state.failures["team.unbind"] = RuntimeError("unbind failed")

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result.ok is True
    assert not session_dir.exists()
    assert ("team.unbind", "research-team", session_id) in delete_env.state.events
    assert session_id not in delete_env.plan.active_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "expected_rollback"),
    [
        (
            "trajectory.begin",
            [("kvc.restore", "failed-agent-session")],
        ),
        (
            "lifecycle.begin",
            [
                ("trajectory.abort", "failed-agent-session"),
                ("kvc.restore", "failed-agent-session"),
            ],
        ),
        (
            "release_subagent",
            [
                ("trajectory.abort", "failed-agent-session"),
                (
                    "lifecycle.abort",
                    "failed-agent-session",
                    "metadata-channel",
                ),
                ("kvc.restore", "failed-agent-session"),
            ],
        ),
        (
            "cleanup",
            [
                ("trajectory.abort", "failed-agent-session"),
                (
                    "lifecycle.abort",
                    "failed-agent-session",
                    "metadata-channel",
                ),
                ("kvc.restore", "failed-agent-session"),
            ],
        ),
        (
            "kvc.evict",
            [
                ("trajectory.abort", "failed-agent-session"),
                (
                    "lifecycle.abort",
                    "failed-agent-session",
                    "metadata-channel",
                ),
                ("kvc.restore", "failed-agent-session"),
            ],
        ),
        (
            "runner.release",
            [
                ("trajectory.abort", "failed-agent-session"),
                (
                    "lifecycle.abort",
                    "failed-agent-session",
                    "metadata-channel",
                ),
                ("kvc.restore", "failed-agent-session"),
            ],
        ),
        (
            "filesystem.remove",
            [
                ("trajectory.abort", "failed-agent-session"),
                (
                    "lifecycle.abort",
                    "failed-agent-session",
                    "metadata-channel",
                ),
                ("kvc.restore", "failed-agent-session"),
            ],
        ),
    ],
)
async def test_non_team_failures_roll_back_and_retain_plan_state(
    delete_env: _DeleteEnvironment,
    failure_stage: str,
    expected_rollback: list[tuple[Any, ...]],
) -> None:
    session_id = "failed-agent-session"
    session_dir = delete_env.create_session(session_id)
    delete_env.state.failures[failure_stage] = RuntimeError(failure_stage)

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result == SessionDeleteResult.failure(
        session_id,
        code="DELETE_FAILED",
        message="session runtime cleanup failed",
    )
    assert delete_env.state.events[-len(expected_rollback) :] == expected_rollback
    assert not any(
        event[0]
        in {
            "trajectory.commit",
            "lifecycle.commit",
            "plan.reset",
            "metadata.remove",
        }
        for event in delete_env.state.events
    )
    assert session_dir.exists()
    assert delete_env.plan.active_sessions == {session_id}
    assert delete_env.plan.exited_sessions == {session_id}


@pytest.mark.asyncio
async def test_cancelled_delete_rolls_back_and_propagates_same_exception(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "cancelled-agent-session"
    session_dir = delete_env.create_session(session_id)
    cancelled = asyncio.CancelledError("caller cancelled")
    delete_env.state.failures["cleanup"] = cancelled

    with pytest.raises(asyncio.CancelledError) as captured:
        await delete_env.runtime.delete_session(
            channel_id="request-channel",
            session_id=session_id,
        )

    assert captured.value is cancelled
    assert delete_env.state.events[-3:] == [
        ("trajectory.abort", session_id),
        ("lifecycle.abort", session_id, "metadata-channel"),
        ("kvc.restore", session_id),
    ]
    assert session_dir.exists()
    assert delete_env.plan.active_sessions == {session_id}
    assert delete_env.plan.exited_sessions == {session_id}
    assert not any(event[0] == "plan.reset" for event in delete_env.state.events)


@pytest.mark.asyncio
async def test_commit_observer_failures_do_not_flip_committed_delete(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "observer-failure-session"
    session_dir = delete_env.create_session(session_id)
    delete_env.state.failures["trajectory.commit"] = RuntimeError(
        "trajectory commit failed"
    )
    delete_env.state.failures["lifecycle.commit"] = RuntimeError(
        "lifecycle commit failed"
    )

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    assert result.ok is True
    assert not session_dir.exists()
    assert delete_env.state.events[-4:] == [
        ("trajectory.commit", session_id),
        ("lifecycle.commit", session_id),
        ("plan.reset", session_id),
        ("metadata.remove", session_id),
    ]
    assert session_id not in delete_env.plan.active_sessions
    assert session_id not in delete_env.plan.exited_sessions


def test_commit_rejects_failed_result_without_mutating_runtime_state(
    delete_env: _DeleteEnvironment,
) -> None:
    session_id = "failed-result"
    delete_env.plan.active_sessions.add(session_id)
    delete_env.plan.exited_sessions.add(session_id)
    result = SessionDeleteResult.failure(
        session_id,
        code="DELETE_FAILED",
        message="session runtime cleanup failed",
    )

    with pytest.raises(ValueError, match="cannot commit a failed session delete"):
        delete_env.runtime.commit_session_delete(result)

    assert delete_env.plan.active_sessions == {session_id}
    assert delete_env.plan.exited_sessions == {session_id}
    assert delete_env.state.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_cleanup", [False, True])
async def test_delete_uses_one_lifecycle_snapshot_for_commit_or_abort(
    delete_env: _DeleteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    fail_cleanup: bool,
) -> None:
    session_id = "lifecycle-snapshot"
    delete_env.create_session(session_id)

    class ReplacementLifecycle(_DeleteLifecycle):
        async def abort_session_delete(
            self,
            session_id: str,
            *,
            channel_id: str = "",
        ) -> None:
            self._state.hit("replacement.abort", session_id, channel_id)

        async def commit_session_delete(self, session_id: str) -> None:
            self._state.hit("replacement.commit", session_id)

    replacement = ReplacementLifecycle(delete_env.state)

    async def begin_and_replace(target: str) -> None:
        delete_env.state.hit("lifecycle.begin", target)
        delete_env.runtime.set_session_delete_lifecycle(replacement)

    monkeypatch.setattr(
        delete_env.lifecycle,
        "begin_session_delete",
        begin_and_replace,
    )
    if fail_cleanup:
        delete_env.state.failures["cleanup"] = RuntimeError("cleanup failed")

    result = await delete_env.runtime.delete_session(
        channel_id="request-channel",
        session_id=session_id,
    )

    if fail_cleanup:
        assert result.ok is False
        assert ("lifecycle.abort", session_id, "metadata-channel") in (
            delete_env.state.events
        )
    else:
        assert result.ok is True
        assert ("lifecycle.commit", session_id) in delete_env.state.events
    assert not any(
        event[0].startswith("replacement.") for event in delete_env.state.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_source", ["path", "metadata"])
async def test_unexpected_path_and_metadata_errors_propagate_unchanged(
    delete_env: _DeleteEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    failure_source: str,
) -> None:
    from jiuwenswarm.server.runtime.session import session_history

    session_id = "unexpected-error-session"
    session_dir = delete_env.create_session(session_id)
    unexpected = RuntimeError(f"unexpected {failure_source} error")
    if failure_source == "path":

        def fail_resolve(*args: object, **kwargs: object) -> None:
            raise unexpected

        monkeypatch.setattr(session_history, "resolve_session_dir", fail_resolve)
    else:
        delete_env.state.failures["metadata"] = unexpected

    with pytest.raises(RuntimeError) as captured:
        await delete_env.runtime.delete_session(
            channel_id="request-channel",
            session_id=session_id,
        )

    assert captured.value is unexpected
    assert delete_env.state.events == (
        []
        if failure_source == "path"
        else [("checkpointer",), ("metadata", session_id)]
    )
    assert session_dir.exists()
    assert delete_env.plan.active_sessions == {session_id}
    assert delete_env.plan.exited_sessions == {session_id}
