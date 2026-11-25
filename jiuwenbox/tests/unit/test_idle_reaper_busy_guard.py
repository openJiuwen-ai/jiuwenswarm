"""空闲回收"忙判定"行为测试。

覆盖：执行中的命令不被回收、后台任务运行中不被回收、后台任务状态无法
确认（守护进程不可达）时按到期回收、状态查询异常时跳过本轮、真闲置照
收、执行中计数在成功/异常/取消/请求构造失败时都能解除、轮询后台任务
刷新活跃时间、守护进程错误匹配不受用户输出干扰。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenbox.models.sandbox import ExecResult, SandboxPhase, SandboxRef
from jiuwenbox.server import sandbox_manager as sm
from jiuwenbox.server.sandbox_manager import (
    SandboxManager,
    _is_daemon_ipc_exec_failure,
)


def _ready_ref(sid: str, *, age_hours: float = 1.0) -> SandboxRef:
    old = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return SandboxRef(
        id=sid,
        phase=SandboxPhase.READY,
        created_at=old,
        started_at=old,
        last_active_at=old,
    )


def _make_manager(tmp_path, *, jobs_state=False, jobs_error=None):
    runtime = SimpleNamespace(
        exec=AsyncMock(return_value=ExecResult(exit_code=0)),
        has_running_background_jobs=AsyncMock(return_value=jobs_state),
        cleanup=AsyncMock(),
        stop=AsyncMock(),
        create=AsyncMock(return_value=123),
    )
    if jobs_error is not None:
        runtime.has_running_background_jobs = AsyncMock(side_effect=jobs_error)
    manager = SandboxManager(
        runtime=runtime,
        audit_logger=Mock(),
        state_dir=tmp_path,
        policy_reader=Mock(),
    )
    manager.policy = SimpleNamespace(
        windows=SimpleNamespace(filesystem=SimpleNamespace(tool_paths=[])),
    )
    return manager, runtime


async def _run_reaper(manager, seconds: float, *, idle_timeout: float = 0.05) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        manager._idle_reaper_loop(
            idle_timeout=idle_timeout,
            check_interval=0.05,
            stop_event=stop,
        ),
    )
    try:
        await asyncio.sleep(seconds)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


def _exec_request():
    return SimpleNamespace(
        command=["true"],
        workdir=None,
        env=None,
        stdin_data=None,
        timeout=None,
    )


# ---- 回收循环 ----


@pytest.mark.asyncio
async def test_reaper_deletes_idle_sandbox(tmp_path):
    manager, runtime = _make_manager(tmp_path, jobs_state=False)
    manager._sandboxes["sb"] = _ready_ref("sb")
    await _run_reaper(manager, 0.4)
    assert "sb" not in manager._sandboxes
    runtime.cleanup.assert_awaited()


@pytest.mark.asyncio
async def test_reaper_skips_inflight_exec(tmp_path):
    manager, runtime = _make_manager(tmp_path, jobs_state=False)
    manager._sandboxes["sb"] = _ready_ref("sb")
    manager._inflight_execs["sb"] = 1
    await _run_reaper(manager, 0.4)
    assert "sb" in manager._sandboxes
    runtime.cleanup.assert_not_awaited()
    runtime.has_running_background_jobs.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_skips_running_background_job(tmp_path):
    manager, runtime = _make_manager(tmp_path, jobs_state=True)
    manager._sandboxes["sb"] = _ready_ref("sb")
    await _run_reaper(manager, 0.4)
    assert "sb" in manager._sandboxes
    runtime.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_deletes_when_daemon_unreachable(tmp_path):
    # 后台任务状态无法确认（守护进程不可达）：沙箱对外已不可用，
    # 按到期回收，不能被陈旧记录挡成"永不回收"。
    manager, runtime = _make_manager(tmp_path, jobs_state=None)
    manager._sandboxes["sb"] = _ready_ref("sb")
    await _run_reaper(manager, 0.4)
    assert "sb" not in manager._sandboxes
    runtime.cleanup.assert_awaited()


@pytest.mark.asyncio
async def test_reaper_skips_when_jobs_check_raises(tmp_path):
    manager, runtime = _make_manager(
        tmp_path, jobs_error=RuntimeError("boom"),
    )
    manager._sandboxes["sb"] = _ready_ref("sb")
    await _run_reaper(manager, 0.4)
    assert "sb" in manager._sandboxes
    runtime.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_keeps_recently_active_sandbox(tmp_path):
    manager, runtime = _make_manager(tmp_path, jobs_state=False)
    manager._sandboxes["sb"] = _ready_ref("sb", age_hours=0.0)
    # TTL 0.5s，观察 0.25s：刚活跃的沙箱不应被回收。
    await _run_reaper(manager, 0.25, idle_timeout=0.5)
    assert "sb" in manager._sandboxes


# ---- 执行中计数 ----


@pytest.mark.asyncio
async def test_exec_counter_released_on_success(tmp_path):
    manager, runtime = _make_manager(tmp_path)
    manager._sandboxes["sb"] = _ready_ref("sb")
    result = await manager.exec_in_sandbox("sb", _exec_request())
    assert result.exit_code == 0
    assert "sb" not in manager._inflight_execs


@pytest.mark.asyncio
async def test_exec_counter_released_on_runtime_error(tmp_path):
    manager, runtime = _make_manager(tmp_path)
    runtime.exec = AsyncMock(side_effect=RuntimeError("boom"))
    ref = _ready_ref("sb")
    manager._sandboxes["sb"] = ref
    with pytest.raises(RuntimeError):
        await manager.exec_in_sandbox("sb", _exec_request())
    assert "sb" not in manager._inflight_execs
    assert ref.last_active_at > ref.started_at


@pytest.mark.asyncio
async def test_exec_counter_released_on_cancel(tmp_path):
    manager, runtime = _make_manager(tmp_path)
    runtime.exec = AsyncMock(side_effect=asyncio.CancelledError)
    manager._sandboxes["sb"] = _ready_ref("sb")
    with pytest.raises(asyncio.CancelledError):
        await manager.exec_in_sandbox("sb", _exec_request())
    assert "sb" not in manager._inflight_execs


@pytest.mark.asyncio
async def test_exec_counter_released_when_request_build_fails(tmp_path, monkeypatch):
    # 计数加一之后、真正执行之前就失败：计数同样要解除。
    manager, runtime = _make_manager(tmp_path)
    manager._sandboxes["sb"] = _ready_ref("sb")

    def boom(*args, **kwargs):
        raise ValueError("bad request")

    monkeypatch.setattr(sm, "RuntimeExecRequest", boom)
    with pytest.raises(ValueError):
        await manager.exec_in_sandbox("sb", _exec_request())
    assert "sb" not in manager._inflight_execs


@pytest.mark.asyncio
async def test_delete_clears_inflight_counter(tmp_path):
    manager, runtime = _make_manager(tmp_path)
    manager._sandboxes["sb"] = _ready_ref("sb")
    manager._inflight_execs["sb"] = 2
    await manager.delete_sandbox("sb")
    assert "sb" not in manager._sandboxes
    assert "sb" not in manager._inflight_execs


# ---- 轮询即活跃 ----


@pytest.mark.asyncio
async def test_background_job_polling_marks_active(tmp_path):
    manager, runtime = _make_manager(tmp_path)
    ref = _ready_ref("sb")
    manager._sandboxes["sb"] = ref
    runtime.get_background_job = AsyncMock(return_value=SimpleNamespace())
    before = ref.last_active_at
    await asyncio.sleep(0.01)
    await manager.get_background_job_in_sandbox("sb", "job1")
    assert ref.last_active_at > before


# ---- 锁外核实窗口的原子收口 ----


@pytest.mark.asyncio
async def test_reaper_survives_exec_started_during_jobs_check(tmp_path):
    # 后台任务核实发生在锁外（阻塞 IPC）。核实期间开始的命令（锁内
    # 标记活跃 + 计数）不得被随后的删除动作误杀。
    manager, runtime = _make_manager(tmp_path, jobs_state=False)
    ref = _ready_ref("sb")
    manager._sandboxes["sb"] = ref

    async def jobs_check(sid):
        async with manager._lock:
            manager._inflight_execs[sid] = manager._inflight_execs.get(sid, 0) + 1
            ref.last_active_at = datetime.now(timezone.utc)
        return False

    runtime.has_running_background_jobs = AsyncMock(side_effect=jobs_check)
    await _run_reaper(manager, 0.4)
    assert "sb" in manager._sandboxes
    runtime.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_if_idle_guards(tmp_path):
    manager, runtime = _make_manager(tmp_path)
    ref = _ready_ref("sb")
    manager._sandboxes["sb"] = ref
    cutoff = ref.last_active_at.timestamp() + 1

    # 命令执行中：不删。
    manager._inflight_execs["sb"] = 1
    assert await manager._delete_if_idle("sb", cutoff) is False
    assert "sb" in manager._sandboxes

    # 刚被使用：不删。
    manager._inflight_execs.pop("sb")
    ref.last_active_at = datetime.now(timezone.utc)
    assert await manager._delete_if_idle("sb", cutoff) is False
    assert "sb" in manager._sandboxes

    # 闲置且无任务：删除并清账。
    ref.last_active_at = ref.started_at
    assert await manager._delete_if_idle("sb", cutoff) is True
    assert "sb" not in manager._sandboxes
    assert "sb" not in manager._inflight_execs
    runtime.cleanup.assert_awaited()


# ---- 运行时忙判定契约 ----


def test_process_runtime_declares_busy_check_contract():
    # 新 RuntimeAdapter 实现必须显式声明忙判定行为：基类默认 None
    # （无法确认→回收器按到期回收），静默继承默认值会让有后台任务的
    # 沙箱被误删。
    from jiuwenbox.server.runtime.base import RuntimeAdapter
    from jiuwenbox.server.runtime.process import ProcessRuntime

    assert (
        ProcessRuntime.has_running_background_jobs
        is not RuntimeAdapter.has_running_background_jobs
    )


# ---- 守护进程错误匹配 ----


def test_daemon_ipc_failure_patterns():
    assert _is_daemon_ipc_exec_failure(
        SimpleNamespace(exit_code=124, stderr="daemon IPC timeout"),
    )
    assert _is_daemon_ipc_exec_failure(
        SimpleNamespace(exit_code=1, stderr="daemon IPC channel unavailable"),
    )
    assert _is_daemon_ipc_exec_failure(
        SimpleNamespace(exit_code=1, stderr="daemon IPC transport failure: socket closed"),
    )
    # 用户命令输出里恰好提到同样文字不应误判（误判会重启并重跑用户命令）。
    assert not _is_daemon_ipc_exec_failure(
        SimpleNamespace(exit_code=1, stderr="job failed, see daemon IPC transport failure logs"),
    )
    assert not _is_daemon_ipc_exec_failure(
        SimpleNamespace(exit_code=1, stderr="custom error"),
    )
