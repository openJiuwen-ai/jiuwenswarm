# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Issue 4268 修复测试。

A1（busy-check 根因修复）：swarmflow 后台 run 存活时，``AgentRuntime.is_session_running``
必须判定会话"运行中"，让 session.delete 在 busy-check 就被 SESSION_BUSY 挡住，
而不是进入 begin() 置 blocked 后死在 cleanup，把会话永久卡在 lifecycle blocked
（guard 拒绝一切受保护请求，前端表现为"切换会话失败"）。

A2（防卡死兜底）：``_recover`` 重试循环对反复失败的 delete/unarchive 有放弃语义——
超过阈值后 ``lc.abandon`` 解除 blocked、置终态 abandoned（不可再自动重试），会话
目录原样保留、用户操作恢复；用户再次显式 delete 会开启全新 operation。失败当刻
栅栏保持（契约见 test_delete_stop_failure_preserves_fence_and_allows_same_operation_retry）。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.session import lifecycle as lc
from tests.unit_tests.server.test_archive_lifecycle import archive  # noqa: F401


# ---------------------------------------------------------------------------
# A1: is_session_running 必须包含 swarmflow 后台 run
# ---------------------------------------------------------------------------


def test_runtime_running_check_includes_swarmflow_runs(monkeypatch):
    """swarmflow 后台 run 跨 leader round 存活，busy-check 必须把它算作运行中。"""
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.model import SessionExecutionState
    from jiuwenswarm.agents.harness.team import team_manager

    snapshot = SimpleNamespace(
        executions=[SimpleNamespace(state=SessionExecutionState.SUCCEEDED)]
    )
    runtime = SimpleNamespace(
        _session_coordinator=SimpleNamespace(snapshot_session=lambda sid: snapshot),
        _pending_chat_requests={},
    )
    # round 已结束、无 inflight 请求，但 swarmflow 后台 run 还在烧：
    # 这种会话在 issue 4268 的日志里正是 delete 误入 begin() 的现场。
    manager = SimpleNamespace(
        has_inflight_request=lambda sid: False,
        is_round_active=lambda sid: False,
        has_swarmflow_runs=lambda sid: True,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    assert AgentRuntime.is_session_running(runtime, "sess_a")
    manager.has_swarmflow_runs = lambda sid: False
    assert not AgentRuntime.is_session_running(runtime, "sess_a")


# ---------------------------------------------------------------------------
# A2: abandon 终态 —— 解除栅栏、允许开新 operation
# ---------------------------------------------------------------------------


def test_abandon_releases_fence_and_allows_new_operation(archive):
    """abandon 后 blocked/write_blocked 解除、guard 放行、begin 开全新 operation。"""
    _, create, root, _ = archive
    create()
    lc.begin("session", "sess_a", "delete")
    lc.update(
        "session", "sess_a", status="failed", errors=["stop failed"], retryable=True
    )
    assert lc.projection("session", "sess_a")["execution_blocked"]
    lc.abandon("session", "sess_a", reason="recovery retries exhausted")
    value = lc.state("session", "sess_a")
    assert value["operation"]["status"] == "abandoned"
    assert value["operation"]["retryable"] is False
    assert value["operation"]["stop_pending"] is False
    assert value["blocked"] is False
    assert value["write_blocked"] is False
    lc.guard("sess_a")
    lc.write_guard("sess_a")
    # 会话数据原样保留：abandon 不移动、不删除任何文件。
    assert (root / "sessions/sess_a").exists()
    # abandoned 与 completed 一样是终态：下一次显式 delete 开全新 operation。
    fresh = lc.begin("session", "sess_a", "delete")
    assert fresh["operation_id"] != value["operation"]["operation_id"]
    assert fresh["status"] == "running"


def test_abandon_ignores_running_operation(archive):
    """running 中的 operation 可能归别的执行者所有，abandon 必须是 no-op。"""
    _, create, _, _ = archive
    create()
    lc.begin("session", "sess_a", "delete")
    lc.abandon("session", "sess_a", reason="race")
    value = lc.state("session", "sess_a")
    assert value["operation"]["status"] == "running"
    assert value["blocked"] is True
    with pytest.raises(lc.LifecycleError) as error:
        lc.guard("sess_a")
    assert error.value.code == "OPERATION_IN_PROGRESS"


@pytest.mark.asyncio
async def test_recover_abandons_repeatedly_failing_delete(archive, monkeypatch):
    """_recover 连续重试超过阈值后放弃：解除 blocked，会话恢复可用。"""
    import jiuwenswarm.server.runtime.session.session_archive as sa

    service, create, root, runtime = archive
    create()
    runtime.stop_session_for_archive.side_effect = RuntimeError(
        "session runtime remains after cleanup"
    )
    with pytest.raises(lc.LifecycleError):
        await service.session("sess_a", "delete", "web")
    assert lc.state("session", "sess_a")["operation"]["status"] == "failed"

    # 快进恢复循环的时钟：跳过全部退避/扫描间隔，但仍让出事件循环。
    real_monotonic, real_sleep = time.monotonic, asyncio.sleep
    clock = {"now": real_monotonic()}

    async def fast_sleep(_seconds):
        clock["now"] += 61.0
        await real_sleep(0)

    monkeypatch.setattr(sa.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    # 时钟快进后租约续约任务会热循环；去掉其文件写放大。
    monkeypatch.setattr(lc, "renew_operation", lambda *args, **kwargs: None)

    task = asyncio.ensure_future(service._recover())
    try:
        for _ in range(500):
            operation = lc.state("session", "sess_a").get("operation") or {}
            if operation.get("status") == "abandoned":
                break
            await real_sleep(0.01)
        value = lc.state("session", "sess_a")
        assert value["operation"]["status"] == "abandoned"
        assert value["blocked"] is False
        lc.guard("sess_a")
        assert (root / "sessions/sess_a").exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# 失败/成功路径回归锚点（修复不得改变既有契约）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_delete_operation_remains_retryable(archive):
    """delete 失败后 operation 保持可重试（_recover 循环仍能接手）。"""
    service, create, _, runtime = archive
    create()
    runtime.stop_session_for_archive.side_effect = RuntimeError("busy")
    with pytest.raises(lc.LifecycleError):
        await service.session("sess_a", "delete", "web")
    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "failed"
    assert operation["retryable"] is True


@pytest.mark.asyncio
async def test_successful_delete_still_blocks_and_deletes(archive):
    """修复不得破坏成功路径：delete 成功仍置 blocked/deleted。"""
    service, create, _, runtime = archive
    create()
    await service.session("sess_a", "delete", "web")
    value = lc.state("session", "sess_a")
    assert value["deleted"] is True
    assert value["blocked"] is True
    with pytest.raises(lc.LifecycleError) as error:
        lc.guard("sess_a")
    assert error.value.code in {"NOT_FOUND", "OPERATION_IN_PROGRESS"}
