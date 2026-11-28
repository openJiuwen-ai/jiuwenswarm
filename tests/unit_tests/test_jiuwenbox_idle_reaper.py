# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for busy-aware idle sandbox reaping in SandboxManager."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

if sys.platform == "win32":
    pytest.skip("jiuwenbox server runs on Linux", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jiuwenbox" / "src"))

from jiuwenbox.models.common import AuditEventType
from jiuwenbox.models.policy import SecurityPolicy, TimeoutPolicy
from jiuwenbox.models.sandbox import (
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    SandboxPhase,
    SandboxRef,
)
from jiuwenbox.server.audit_logger import AuditLogger
from jiuwenbox.server.policy_engine import PolicyEngine
from jiuwenbox.server.policy_reader import PolicyReader
from jiuwenbox.server.runtime.base import (
    RuntimeAdapter,
    RuntimeBackgroundExecRequest,
    RuntimeExecRequest,
    RuntimeFileOpResult,
)
from jiuwenbox.server.runtime.errors import SandboxStateError
from jiuwenbox.server.runtime.process import ProcessRuntime
from jiuwenbox.server.sandbox_manager import (
    SandboxExecRequest,
    SandboxListRequest,
    SandboxManager,
    _is_daemon_ipc_exec_failure,
)


class _FakeRuntime(RuntimeAdapter):
    """Minimal in-memory runtime adapter for deterministic reaper tests."""

    def __init__(self) -> None:
        self.exec_handler = None
        self.list_jobs_handler = None
        self.get_job_handler = None
        self.write_file_handler = None
        self.read_file_handler = None
        self.list_dir_handler = None
        self.search_files_handler = None
        self.cleaned_up: list[str] = []
        self.jobs: dict[str, list[BackgroundJobSummary]] = {}

    async def create(
        self,
        sandbox_id: str,
        policy_path: Path,
        env: dict[str, str] | None = None,
    ) -> int:
        return 12345

    async def start(self, sandbox_id: str) -> int:
        return 12345

    async def stop(self, sandbox_id: str) -> None:
        return None

    async def cleanup(self, sandbox_id: str) -> None:
        self.cleaned_up.append(sandbox_id)

    async def exec(
        self,
        sandbox_id: str,
        request: RuntimeExecRequest,
    ) -> ExecResult:
        if self.exec_handler is not None:
            return await self.exec_handler(sandbox_id, request)
        return ExecResult(exit_code=0, stdout="ok", stderr="")

    async def exec_background(
        self,
        sandbox_id: str,
        request: RuntimeBackgroundExecRequest,
    ):
        raise NotImplementedError

    async def get_background_job(
        self,
        sandbox_id: str,
        job_id: str,
    ) -> BackgroundJobStatus:
        if self.get_job_handler is not None:
            return await self.get_job_handler(sandbox_id, job_id)
        now = datetime.now(timezone.utc)
        return BackgroundJobStatus(
            job_id=job_id,
            sandbox_id=sandbox_id,
            command=["sleep", "10"],
            pid=999,
            running=True,
            exit_code=None,
            started_at=now,
            finished_at=None,
        )

    async def list_background_jobs(
        self,
        sandbox_id: str,
        *,
        running_only: bool = False,
    ) -> list[BackgroundJobSummary]:
        if self.list_jobs_handler is not None:
            return await self.list_jobs_handler(sandbox_id, running_only=running_only)
        items = list(self.jobs.get(sandbox_id, []))
        if running_only:
            return [item for item in items if item.running]
        return items

    async def kill_background_job(
        self,
        sandbox_id: str,
        job_id: str,
        signum: int = 15,
    ):
        raise NotImplementedError

    async def write_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
        *,
        mkdir_parents: bool = True,
    ) -> RuntimeFileOpResult:
        if self.write_file_handler is not None:
            return await self.write_file_handler(
                sandbox_id,
                sandbox_path,
                content,
                mkdir_parents=mkdir_parents,
            )
        return RuntimeFileOpResult(ok=True)

    async def read_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
    ) -> RuntimeFileOpResult:
        if self.read_file_handler is not None:
            return await self.read_file_handler(sandbox_id, sandbox_path)
        return RuntimeFileOpResult(ok=True, content=b"data")

    async def list_dir(
        self,
        sandbox_id: str,
        sandbox_path: str,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        include_files: bool = True,
        include_dirs: bool = True,
    ) -> RuntimeFileOpResult:
        if self.list_dir_handler is not None:
            return await self.list_dir_handler(sandbox_id, sandbox_path)
        return RuntimeFileOpResult(ok=True, items=[])

    async def search_files(
        self,
        sandbox_id: str,
        sandbox_path: str,
        patterns: list[str],
        *,
        exclude_patterns: list[str] | None = None,
    ) -> RuntimeFileOpResult:
        if self.search_files_handler is not None:
            return await self.search_files_handler(sandbox_id, sandbox_path)
        return RuntimeFileOpResult(ok=True, items=[])

    async def get_status(self, sandbox_id: str) -> str:
        return "running"

    async def is_running(self, sandbox_id: str) -> bool:
        return True

    async def get_logs(self, sandbox_id: str) -> str:
        return ""


def _build_manager(
    tmp_path: Path,
    runtime: RuntimeAdapter | None = None,
    *,
    idle_timeout: float | None = 180.0,
    idle_check_interval: float = 60.0,
) -> tuple[SandboxManager, _FakeRuntime | RuntimeAdapter]:
    fake_runtime = runtime if runtime is not None else _FakeRuntime()
    policy_engine = PolicyEngine(policies_dir=tmp_path / "policies")
    audit_logger = AuditLogger(log_dir=tmp_path / "audit", filename_strategy="plain")
    policy_file = tmp_path / "default_policy.yaml"
    policy = SecurityPolicy(
        name="reaper-test",
        timeout=TimeoutPolicy(
            idle_timeout=idle_timeout,
            idle_check_interval=idle_check_interval,
        ),
    )
    policy_file.write_text(policy.model_dump_json(), encoding="utf-8")
    policy_reader = PolicyReader(policy_engine=policy_engine, policy_path=policy_file)
    manager = SandboxManager(
        runtime=fake_runtime,
        policy_engine=policy_engine,
        audit_logger=audit_logger,
        state_dir=tmp_path / "sandboxes",
        policy_reader=policy_reader,
    )
    return manager, fake_runtime


def _register_sandbox(
    manager: SandboxManager,
    sandbox_id: str,
    *,
    age_seconds: float,
    phase: SandboxPhase = SandboxPhase.READY,
) -> SandboxRef:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ref = SandboxRef(
        id=sandbox_id,
        phase=phase,
        runtime="process",
        pid=12345,
        created_at=ts,
        started_at=ts,
        last_active_at=ts,
    )
    manager._sandboxes[sandbox_id] = ref
    manager._save_state(ref)
    return ref


@pytest.mark.asyncio
async def test_idle_reaper_skips_inflight_sync_exec_and_reaps_after_post_completion_ttl(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    ref = _register_sandbox(manager, "sb-sync-long", age_seconds=0.0)

    exec_started = asyncio.Event()
    exec_release = asyncio.Event()

    async def slow_exec(sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        exec_started.set()
        await exec_release.wait()
        return ExecResult(exit_code=0, stdout="done", stderr="")

    runtime.exec_handler = slow_exec
    exec_task = asyncio.create_task(
        manager.exec_in_sandbox("sb-sync-long", SandboxExecRequest(command=["sleep", "300"]))
    )
    await exec_started.wait()

    # Simulate elapsed time > idle_timeout while the sync exec is still running.
    stale_ts = datetime.now(timezone.utc) - timedelta(seconds=120.0)
    ref.last_active_at = stale_ts

    with caplog.at_level(logging.INFO):
        first_sweep = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
        second_sweep = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)

    assert first_sweep == []
    assert second_sweep == []
    assert "sb-sync-long" in manager._sandboxes
    assert runtime.cleaned_up == []
    skip_logs = [
        record.message
        for record in caplog.records
        if "sb-sync-long" in record.message and "reason=in_flight_ops" in record.message
    ]
    assert len(skip_logs) == 2

    # Finish the long exec; completion must refresh last_active_at so an
    # immediate follow-up sweep does not reap the sandbox right after return.
    exec_release.set()
    result = await exec_task
    assert result.exit_code == 0
    assert result.stdout == "done"
    assert manager._inflight_ops.get("sb-sync-long", 0) == 0
    assert ref.last_active_at is not None
    assert ref.last_active_at.astimezone(timezone.utc) > stale_ts

    immediate_sweep = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
    assert immediate_sweep == []
    assert "sb-sync-long" in manager._sandboxes

    # Once idle again past TTL, the sandbox is reaped cleanly.
    ref.last_active_at = datetime.now(timezone.utc) - timedelta(seconds=120.0)
    final_sweep = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
    assert final_sweep == ["sb-sync-long"]
    assert "sb-sync-long" not in manager._sandboxes
    assert runtime.cleaned_up == ["sb-sync-long"]


@pytest.mark.asyncio
async def test_idle_reaper_skips_running_background_job_and_respects_finished_grace(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    ref = _register_sandbox(manager, "sb-bg-long", age_seconds=120.0)
    started_at = datetime.now(timezone.utc) - timedelta(seconds=120.0)
    runtime.jobs["sb-bg-long"] = [
        BackgroundJobSummary(
            job_id="job-1",
            pid=4321,
            command=["python3", "parse.py"],
            running=True,
            exit_code=None,
            started_at=started_at,
            finished_at=None,
        )
    ]

    with caplog.at_level(logging.INFO):
        sweep_1 = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
        sweep_2 = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)

    assert sweep_1 == []
    assert sweep_2 == []
    assert "sb-bg-long" in manager._sandboxes
    skip_logs = [
        record.message
        for record in caplog.records
        if "sb-bg-long" in record.message and "reason=running_background_jobs" in record.message
    ]
    assert len(skip_logs) == 2

    # Background job just finished 2 seconds ago: sandbox gets full TTL grace.
    recent_finish = datetime.now(timezone.utc) - timedelta(seconds=2.0)
    runtime.jobs["sb-bg-long"] = [
        BackgroundJobSummary(
            job_id="job-1",
            pid=4321,
            command=["python3", "parse.py"],
            running=False,
            exit_code=0,
            started_at=started_at,
            finished_at=recent_finish,
        )
    ]
    grace_sweep = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
    assert grace_sweep == []
    assert "sb-bg-long" in manager._sandboxes
    assert ref.last_active_at is not None
    assert abs(ref.last_active_at.astimezone(timezone.utc).timestamp() - recent_finish.timestamp()) < 1.0

    # Once job completion ages past idle_timeout, sandbox is reaped cleanly.
    old_finish = datetime.now(timezone.utc) - timedelta(seconds=120.0)
    ref.last_active_at = old_finish
    runtime.jobs["sb-bg-long"] = [
        BackgroundJobSummary(
            job_id="job-1",
            pid=4321,
            command=["python3", "parse.py"],
            running=False,
            exit_code=0,
            started_at=started_at,
            finished_at=old_finish,
        )
    ]
    reap_sweep = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
    assert reap_sweep == ["sb-bg-long"]
    assert "sb-bg-long" not in manager._sandboxes
    assert runtime.cleaned_up == ["sb-bg-long"]


@pytest.mark.asyncio
async def test_background_job_query_and_list_refresh_activity_without_breaking_stopped_queries(
    tmp_path: Path,
) -> None:
    manager, _ = _build_manager(tmp_path, idle_timeout=60.0)
    stale_ts = datetime.now(timezone.utc) - timedelta(seconds=120.0)
    ready_ref = _register_sandbox(manager, "sb-poll", age_seconds=120.0)
    ready_ref.last_active_at = stale_ts

    status = await manager.get_background_job_in_sandbox("sb-poll", "job-1")
    assert status.job_id == "job-1"
    assert ready_ref.last_active_at.astimezone(timezone.utc) > stale_ts

    ready_ref.last_active_at = stale_ts
    jobs = await manager.list_background_jobs_in_sandbox("sb-poll", running_only=False)
    assert jobs == []
    assert ready_ref.last_active_at.astimezone(timezone.utc) > stale_ts

    # Stopped sandboxes remain queryable without raising SandboxStateError.
    stopped_ref = _register_sandbox(
        manager,
        "sb-stopped",
        age_seconds=120.0,
        phase=SandboxPhase.STOPPED,
    )
    stopped_ref.last_active_at = stale_ts
    stopped_jobs = await manager.list_background_jobs_in_sandbox("sb-stopped")
    assert stopped_jobs == []
    assert stopped_ref.last_active_at == stale_ts


@pytest.mark.asyncio
async def test_idle_reaper_loop_still_reaps_truly_idle_sandbox(tmp_path: Path) -> None:
    manager, runtime = _build_manager(
        tmp_path,
        idle_timeout=0.1,
        idle_check_interval=0.05,
    )
    _register_sandbox(manager, "sb-truly-idle", age_seconds=10.0)

    manager.start_idle_reaper()
    try:
        for _ in range(30):
            if "sb-truly-idle" not in manager._sandboxes:
                break
            await asyncio.sleep(0.05)
    finally:
        await manager.stop_idle_reaper()

    assert "sb-truly-idle" not in manager._sandboxes
    assert runtime.cleaned_up == ["sb-truly-idle"]
    manager.audit.flush()
    audit_events = manager.audit.read_logs("sb-truly-idle")
    deleted_events = [
        event for event in audit_events if event.event_type == AuditEventType.SANDBOX_DELETED
    ]
    assert len(deleted_events) == 1
    assert deleted_events[0].details.get("reason") == "idle_timeout"


@pytest.mark.asyncio
async def test_idle_reaper_aborts_delete_when_new_exec_starts_during_bg_probe(
    tmp_path: Path,
) -> None:
    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    _register_sandbox(manager, "sb-race", age_seconds=120.0)

    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()
    exec_entered = asyncio.Event()
    exec_release = asyncio.Event()

    async def slow_list_jobs(sandbox_id: str, *, running_only: bool = False):
        probe_entered.set()
        await probe_release.wait()
        return []

    async def slow_exec(sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        exec_entered.set()
        await exec_release.wait()
        return ExecResult(exit_code=0, stdout="raced-ok", stderr="")

    runtime.list_jobs_handler = slow_list_jobs
    runtime.exec_handler = slow_exec

    reap_task = asyncio.create_task(manager._reap_idle_sandboxes_once(idle_timeout=60.0))
    await probe_entered.wait()

    # A new exec arrives while the reaper is outside self._lock probing bg jobs.
    exec_task = asyncio.create_task(
        manager.exec_in_sandbox("sb-race", SandboxExecRequest(command=["echo", "hi"]))
    )
    await exec_entered.wait()

    # Let the reaper finish probing; atomic re-check must abort deletion.
    probe_release.set()
    reaped = await reap_task
    assert reaped == []
    assert "sb-race" in manager._sandboxes
    assert manager._sandboxes["sb-race"].phase == SandboxPhase.READY
    assert runtime.cleaned_up == []

    exec_release.set()
    result = await exec_task
    assert result.exit_code == 0
    assert result.stdout == "raced-ok"


@pytest.mark.asyncio
async def test_idle_reaper_reaps_dead_runtime_with_stale_running_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_runtime = ProcessRuntime()
    monkeypatch.setattr(process_runtime, "is_running", AsyncMock(return_value=False))
    cleanup_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(process_runtime, "cleanup", cleanup_mock)
    list_jobs_mock = AsyncMock(
        return_value=[
            BackgroundJobSummary(
                job_id="stale-job",
                pid=1111,
                command=["sleep", "999"],
                running=True,
                exit_code=None,
                started_at=datetime.now(timezone.utc) - timedelta(seconds=300),
                finished_at=None,
            )
        ]
    )
    monkeypatch.setattr(process_runtime, "list_background_jobs", list_jobs_mock)

    manager, _ = _build_manager(tmp_path, runtime=process_runtime, idle_timeout=60.0)
    _register_sandbox(manager, "sb-dead-daemon", age_seconds=120.0)

    reaped = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
    assert reaped == ["sb-dead-daemon"]
    assert "sb-dead-daemon" not in manager._sandboxes
    cleanup_mock.assert_awaited_once_with("sb-dead-daemon")
    list_jobs_mock.assert_awaited_once_with("sb-dead-daemon", running_only=False)


@pytest.mark.asyncio
async def test_idle_reaper_skips_probe_failure_without_blocking_other_sandboxes(
    tmp_path: Path,
) -> None:
    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    _register_sandbox(manager, "sb-probe-err", age_seconds=120.0)
    _register_sandbox(manager, "sb-idle-ok", age_seconds=120.0)

    async def flaky_list_jobs(sandbox_id: str, *, running_only: bool = False):
        if sandbox_id == "sb-probe-err":
            raise RuntimeError("transient probe failure")
        return []

    runtime.list_jobs_handler = flaky_list_jobs

    reaped = await manager._reap_idle_sandboxes_once(idle_timeout=60.0)
    assert reaped == ["sb-idle-ok"]
    assert "sb-probe-err" in manager._sandboxes
    assert "sb-idle-ok" not in manager._sandboxes


@pytest.mark.asyncio
async def test_explicit_delete_still_terminates_inflight_exec_without_spurious_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    _register_sandbox(manager, "sb-explicit-del", age_seconds=0.0)

    exec_started = asyncio.Event()
    exec_release = asyncio.Event()
    restart_mock = AsyncMock()
    monkeypatch.setattr(manager, "restart_sandbox", restart_mock)

    async def interrupted_exec(sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        exec_started.set()
        await exec_release.wait()
        return ExecResult(
            exit_code=1,
            stdout="",
            stderr="daemon IPC unavailable: ENOENT",
        )

    runtime.exec_handler = interrupted_exec
    exec_task = asyncio.create_task(
        manager.exec_in_sandbox("sb-explicit-del", SandboxExecRequest(command=["sleep", "60"]))
    )
    await exec_started.wait()

    await manager.delete_sandbox("sb-explicit-del")
    assert "sb-explicit-del" not in manager._sandboxes
    assert runtime.cleaned_up == ["sb-explicit-del"]

    exec_release.set()
    result = await exec_task
    assert result.exit_code == 1
    assert _is_daemon_ipc_exec_failure(result)
    restart_mock.assert_not_awaited()
    assert manager._inflight_ops.get("sb-explicit-del", 0) == 0


@pytest.mark.asyncio
async def test_daemon_ipc_exec_failure_matcher_and_ready_restart_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _is_daemon_ipc_exec_failure(
        ExecResult(exit_code=1, stderr="daemon IPC unavailable: ECONNREFUSED")
    )
    assert _is_daemon_ipc_exec_failure(
        ExecResult(exit_code=1, stderr="sandbox 'x' daemon IPC channel unavailable")
    )
    assert _is_daemon_ipc_exec_failure(
        ExecResult(exit_code=124, stderr="daemon IPC timeout: timed out")
    )
    # Mid-exec transport failures and cgroup OOM kills must not blindly
    # re-execute potentially non-idempotent commands.
    assert not _is_daemon_ipc_exec_failure(
        ExecResult(exit_code=1, stderr="daemon IPC transport failure: broken pipe")
    )
    assert not _is_daemon_ipc_exec_failure(
        ExecResult(exit_code=137, stderr="Killed\n")
    )
    assert not _is_daemon_ipc_exec_failure(
        ExecResult(exit_code=1, stderr="command failed normally")
    )

    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    _register_sandbox(manager, "sb-retry", age_seconds=0.0)

    attempts = {"count": 0}

    async def flaky_exec(sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr="daemon IPC unavailable: ECONNREFUSED",
            )
        return ExecResult(exit_code=0, stdout="recovered", stderr="")

    restart_mock = AsyncMock(return_value=manager._sandboxes["sb-retry"])
    monkeypatch.setattr(manager, "restart_sandbox", restart_mock)
    runtime.exec_handler = flaky_exec

    result = await manager.exec_in_sandbox("sb-retry", SandboxExecRequest(command=["echo", "hi"]))
    assert result.exit_code == 0
    assert result.stdout == "recovered"
    assert attempts["count"] == 2
    restart_mock.assert_awaited_once_with("sb-retry")
    assert manager._inflight_ops.get("sb-retry", 0) == 0


@pytest.mark.asyncio
async def test_restart_sandbox_rejects_deleting_sandbox(tmp_path: Path) -> None:
    manager, _ = _build_manager(tmp_path, idle_timeout=60.0)
    _register_sandbox(
        manager,
        "sb-deleting",
        age_seconds=0.0,
        phase=SandboxPhase.DELETING,
    )

    with pytest.raises(SandboxStateError, match="Cannot restart sandbox 'sb-deleting'"):
        await manager.restart_sandbox("sb-deleting")
    assert manager._sandboxes["sb-deleting"].phase == SandboxPhase.DELETING


@pytest.mark.asyncio
async def test_inflight_counter_cleans_up_on_error_and_cancellation(tmp_path: Path) -> None:
    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    _register_sandbox(manager, "sb-cleanup", age_seconds=0.0)

    async def failing_exec(sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        raise RuntimeError("boom")

    runtime.exec_handler = failing_exec
    with pytest.raises(RuntimeError, match="boom"):
        await manager.exec_in_sandbox("sb-cleanup", SandboxExecRequest(command=["false"]))
    assert manager._inflight_ops.get("sb-cleanup", 0) == 0

    started = asyncio.Event()

    async def hanging_exec(sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        started.set()
        await asyncio.sleep(60)
        return ExecResult(exit_code=0)

    runtime.exec_handler = hanging_exec
    task = asyncio.create_task(
        manager.exec_in_sandbox("sb-cleanup", SandboxExecRequest(command=["sleep", "60"]))
    )
    await started.wait()
    assert manager._inflight_ops.get("sb-cleanup", 0) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager._inflight_ops.get("sb-cleanup", 0) == 0


@pytest.mark.asyncio
async def test_file_ops_protect_sandbox_while_inflight_and_handle_nested_fallback(
    tmp_path: Path,
) -> None:
    manager, runtime = _build_manager(tmp_path, idle_timeout=60.0)
    ref = _register_sandbox(manager, "sb-files", age_seconds=0.0)

    fallback_started = asyncio.Event()
    fallback_release = asyncio.Event()

    async def unsupported_write(
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
        *,
        mkdir_parents: bool = True,
    ) -> RuntimeFileOpResult:
        return RuntimeFileOpResult(ok=False, error="unsupported")

    async def nested_exec(sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        fallback_started.set()
        # Both upload_file_to_sandbox and nested exec_in_sandbox are tracked.
        assert manager._inflight_ops.get(sandbox_id) == 2
        await fallback_release.wait()
        return ExecResult(exit_code=0, stdout="", stderr="")

    runtime.write_file_handler = unsupported_write
    runtime.exec_handler = nested_exec

    upload_task = asyncio.create_task(
        manager.upload_file_to_sandbox("sb-files", "/tmp/a.txt", b"hello")
    )
    await fallback_started.wait()
    ref.last_active_at = datetime.now(timezone.utc) - timedelta(seconds=120.0)
    assert await manager._reap_idle_sandboxes_once(idle_timeout=60.0) == []

    fallback_release.set()
    await upload_task
    assert manager._inflight_ops.get("sb-files", 0) == 0

    assert await manager.download_file_from_sandbox("sb-files", "/tmp/a.txt") == b"data"
    assert await manager.list_files_in_sandbox("sb-files", SandboxListRequest(sandbox_path="/tmp")) == []
    assert await manager.search_files_in_sandbox("sb-files", "/tmp", "*.txt") == []
    assert manager._inflight_ops.get("sb-files", 0) == 0


@pytest.mark.asyncio
async def test_idle_reaper_isolates_corrupt_sandbox_and_loop_survives_sweep_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, runtime = _build_manager(
        tmp_path,
        idle_timeout=0.1,
        idle_check_interval=0.05,
    )
    _register_sandbox(manager, "sb-corrupt", age_seconds=10.0)
    _register_sandbox(manager, "sb-healthy", age_seconds=10.0)

    original_runtime_for = manager._runtime_for

    def flaky_runtime_for(ref: SandboxRef) -> RuntimeAdapter:
        if ref.id == "sb-corrupt":
            raise RuntimeError("corrupt runtime ref")
        return original_runtime_for(ref)

    monkeypatch.setattr(manager, "_runtime_for", flaky_runtime_for)
    reaped = await manager._reap_idle_sandboxes_once(idle_timeout=0.1)
    assert reaped == ["sb-healthy"]
    assert "sb-healthy" not in manager._sandboxes
    assert "sb-corrupt" in manager._sandboxes

    # Also verify _idle_reaper_loop stays alive after an unexpected sweep error.
    monkeypatch.setattr(manager, "_runtime_for", original_runtime_for)
    manager._sandboxes.pop("sb-corrupt", None)
    _register_sandbox(manager, "sb-after-err", age_seconds=10.0)

    original_sweep = manager._reap_idle_sandboxes_once
    attempts = {"count": 0}

    async def flaky_sweep(*, idle_timeout: float) -> list[str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient sweep crash")
        return await original_sweep(idle_timeout=idle_timeout)

    monkeypatch.setattr(manager, "_reap_idle_sandboxes_once", flaky_sweep)
    manager.start_idle_reaper()
    try:
        for _ in range(30):
            if "sb-after-err" not in manager._sandboxes:
                break
            await asyncio.sleep(0.05)
    finally:
        await manager.stop_idle_reaper()

    assert attempts["count"] >= 2
    assert "sb-after-err" not in manager._sandboxes
    assert "sb-after-err" in runtime.cleaned_up
