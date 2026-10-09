from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager import (
    DeepResearchManagerClosedError,
    DeepResearchTaskManager,
    DeepResearchTaskManagerPool,
)
from jiuwenswarm.common.local_env_config import (
    bind_task_env_overlay,
    reset_task_env_overlay,
)
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey


def _process(*, returncode=None, wait_side_effect=None):
    process = MagicMock()
    process.returncode = returncode
    if wait_side_effect is None:
        process.wait = AsyncMock(return_value=0)
    else:
        process.wait = AsyncMock(side_effect=wait_side_effect)
    return process


class _LifecycleProcess:
    def __init__(
        self,
        pid: int,
        *,
        terminate_error: BaseException | None = None,
        kill_error: BaseException | None = None,
        wait_results: list[BaseException | int] | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = None
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.wait_results = list(wait_results or [0])
        self.calls: list[str] = []

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self) -> None:
        self.calls.append("kill")
        if self.kill_error is not None:
            raise self.kill_error

    async def wait(self) -> int:
        self.calls.append("wait")
        result = self.wait_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        self.returncode = result
        return result


class _BlockingWaitProcess(_LifecycleProcess):
    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.wait_started = asyncio.Event()
        self.release_wait = asyncio.Event()

    async def wait(self) -> int:
        self.calls.append("wait")
        self.wait_started.set()
        await self.release_wait.wait()
        self.returncode = 0
        return 0


class _NeverExitsProcess(_LifecycleProcess):
    async def wait(self) -> int:
        self.calls.append("wait")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _CancelDuringFirstWaitProcess(_LifecycleProcess):
    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.first_wait_started = asyncio.Event()

    async def wait(self) -> int:
        self.calls.append("wait")
        if self.calls.count("wait") == 1:
            self.first_wait_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        self.returncode = 0
        return 0


def test_manager_normalizes_tenant_and_tracks_process_sets_by_session():
    manager = DeepResearchTaskManager(service_id="  ", agent_id="\t")
    first = _process()
    second = _process()

    manager.track_process("session-a", first)
    manager.track_process("session-a", second)
    manager.track_process("session-b", first)

    assert manager.service_id == "default"
    assert manager.agent_id == "default"
    assert manager._processes == {
        "session-a": {first, second},
        "session-b": {first},
    }

    manager.untrack_process("session-a", first)
    assert manager._processes["session-a"] == {second}
    manager.untrack_process("session-a", second)
    assert "session-a" not in manager._processes
    manager.untrack_process("session-a", second)
    manager.untrack_process("missing-session", first)


def test_pool_isolates_runtime_scopes_by_workspace_key():
    DeepResearchTaskManagerPool.reset_for_tests()
    try:
        workspace_a = RuntimeScopeKey.from_ids(
            "svc",
            "agent",
            workspace_key="workspace-a",
        )
        workspace_b = RuntimeScopeKey.from_ids(
            "svc",
            "agent",
            workspace_key="workspace-b",
        )

        manager_a = DeepResearchTaskManagerPool.get_or_create_sync(workspace_a)
        manager_b = DeepResearchTaskManagerPool.get_or_create_sync(workspace_b)

        assert manager_a is not manager_b
        assert DeepResearchTaskManagerPool.get_or_create_sync(workspace_a) is manager_a
        assert DeepResearchTaskManagerPool.get_or_create_sync(workspace_b) is manager_b
    finally:
        DeepResearchTaskManagerPool.reset_for_tests()


def test_shutdown_terminates_active_processes_across_sessions_and_clears_registry():
    manager = DeepResearchTaskManager(service_id="svc", agent_id="agent")
    first = _process()
    second = _process()
    exited = _process(returncode=0)
    manager.track_process("session-a", first)
    manager.track_process("session-b", second)
    manager.track_process("session-b", exited)

    asyncio.run(manager.shutdown(timeout=0.01))

    first.terminate.assert_called_once_with()
    second.terminate.assert_called_once_with()
    first.wait.assert_awaited_once()
    second.wait.assert_awaited_once()
    first.kill.assert_not_called()
    second.kill.assert_not_called()
    exited.terminate.assert_not_called()
    exited.kill.assert_not_called()
    exited.wait.assert_not_awaited()
    assert manager._processes == {}


def test_shutdown_stops_same_process_registered_in_multiple_sessions_once():
    manager = DeepResearchTaskManager()
    process = _process()
    manager.track_process("session-a", process)
    manager.track_process("session-b", process)

    asyncio.run(manager.shutdown(timeout=0.01))

    process.terminate.assert_called_once_with()
    process.wait.assert_awaited_once()
    process.kill.assert_not_called()
    assert manager._processes == {}


def test_shutdown_finishes_child_cleanup_before_propagating_cancellation():
    manager = DeepResearchTaskManager()
    process = _CancelDuringFirstWaitProcess(601)
    manager.track_process("session", process)

    async def _run() -> None:
        shutdown_task = asyncio.create_task(manager.shutdown(timeout=0.01))
        await process.first_wait_started.wait()
        shutdown_task.cancel()
        await asyncio.wait_for(shutdown_task, timeout=0.1)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert process.calls == ["terminate", "wait", "kill", "wait"]
    assert manager._processes == {}
    assert manager._closing is True


def test_shutdown_resists_repeated_cancellation_until_child_cleanup_finishes():
    manager = DeepResearchTaskManager()
    process = _CancelDuringFirstWaitProcess(602)
    manager.track_process("session", process)

    async def _run() -> None:
        cancel_handler_waiting = asyncio.Event()
        original_shield = asyncio.shield

        async def _observed_shield(awaitable):
            try:
                return await original_shield(awaitable)
            except asyncio.CancelledError:
                cancel_handler_waiting.set()
                raise

        with patch(
            "jiuwenswarm.agents.harness.common.tools."
            "deepresearch_task_manager.asyncio.shield",
            new=_observed_shield,
        ):
            shutdown_task = asyncio.create_task(manager.shutdown(timeout=0.01))
            await process.first_wait_started.wait()
            shutdown_task.cancel()
            await cancel_handler_waiting.wait()
            shutdown_task.cancel()
            await asyncio.wait_for(shutdown_task, timeout=0.1)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert process.calls == ["terminate", "wait", "kill", "wait"]
    assert manager._processes == {}
    assert manager._closing is True


def test_shutdown_kills_process_after_terminate_wait_timeout():
    manager = DeepResearchTaskManager()
    process = _process(wait_side_effect=[asyncio.TimeoutError, 0])
    manager.track_process("session", process)

    asyncio.run(manager.shutdown(timeout=0.01))

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.await_count == 2
    assert manager._processes == {}


def test_shutdown_continues_after_terminate_process_lookup():
    manager = DeepResearchTaskManager()
    missing = _LifecycleProcess(101, terminate_error=ProcessLookupError())
    survivor = _LifecycleProcess(102)
    manager.track_process("first", missing)
    manager.track_process("second", survivor)

    asyncio.run(manager.shutdown(timeout=0.01))

    assert missing.calls == ["terminate"]
    assert survivor.calls == ["terminate", "wait"]
    assert manager._processes == {}


def test_shutdown_continues_after_kill_process_lookup():
    manager = DeepResearchTaskManager()
    missing = _LifecycleProcess(
        201,
        kill_error=ProcessLookupError(),
        wait_results=[asyncio.TimeoutError()],
    )
    survivor = _LifecycleProcess(202)
    manager.track_process("first", missing)
    manager.track_process("second", survivor)

    asyncio.run(manager.shutdown(timeout=0.01))

    assert missing.calls == ["terminate", "wait", "kill"]
    assert survivor.calls == ["terminate", "wait"]
    assert manager._processes == {}


def test_shutdown_continues_after_second_wait_timeout():
    manager = DeepResearchTaskManager()
    stuck = _NeverExitsProcess(301)
    survivor = _LifecycleProcess(302)
    manager.track_process("first", stuck)
    manager.track_process("second", survivor)

    asyncio.run(asyncio.wait_for(manager.shutdown(timeout=0.01), timeout=0.1))

    assert stuck.calls == ["terminate", "wait", "kill", "wait"]
    assert survivor.calls == ["terminate", "wait"]
    assert manager._processes == {}


def test_shutdown_logs_unexpected_cleanup_error_and_continues():
    manager = DeepResearchTaskManager()
    broken = _LifecycleProcess(401, terminate_error=RuntimeError("do-not-log-this"))
    survivor = _LifecycleProcess(402)
    manager.track_process("first", broken)
    manager.track_process("second", survivor)

    with patch(
        "jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager.logger.warning"
    ) as warning:
        asyncio.run(manager.shutdown(timeout=0.01))

    assert broken.calls == ["terminate"]
    assert survivor.calls == ["terminate", "wait"]
    warning.assert_called_once()
    warning_args = warning.call_args.args
    assert warning_args[1:] == ("terminate", "RuntimeError", 401)
    assert "do-not-log-this" not in repr(warning.call_args)
    assert manager._processes == {}


def test_shutdown_rejects_late_registration_and_never_reopens():
    manager = DeepResearchTaskManager()
    active = _BlockingWaitProcess(501)
    late = _LifecycleProcess(502)
    manager.track_process("active", active)

    async def _run() -> tuple[BaseException | None, BaseException | None]:
        shutdown_task = asyncio.create_task(manager.shutdown(timeout=0.1))
        await active.wait_started.wait()
        try:
            manager.track_process("late", late)
        except DeepResearchManagerClosedError as exc:
            first_error = exc
        else:
            first_error = None
        active.release_wait.set()
        await shutdown_task

        await manager.shutdown(timeout=0.1)
        try:
            manager.track_process("later", late)
        except DeepResearchManagerClosedError as exc:
            second_error = exc
        else:
            second_error = None
        return first_error, second_error

    first_error, second_error = asyncio.run(_run())

    for error in (first_error, second_error):
        assert isinstance(error, DeepResearchManagerClosedError)
        assert str(error) == "deepresearch_manager_closed"
    assert active.calls == ["terminate", "wait"]
    assert late.calls == []
    assert manager._processes == {}


def test_load_config_prefers_detected_bocha_and_explicit_petal():
    from jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager import (
        load_deepresearch_config,
    )

    common = {
        "MODEL_NAME": "global-model",
        "MODEL_PROVIDER": "openai",
        "API_BASE": "https://maas.example/v2/",
        "API_KEY": "global-key",
        "BOCHA_API_KEY": "bocha-key",
    }

    detected = load_deepresearch_config(common)
    assert detected["WEB_SEARCH_ENGINE_NAME"] == "bocha"
    assert detected["WEB_SEARCH_API_KEY"] == "bocha-key"

    explicit_petal = load_deepresearch_config(
        {
            **common,
            "WEB_SEARCH_ENGINE_NAME": "petal",
            "PETAL_API_KEY": "petal-key",
        }
    )
    assert explicit_petal["WEB_SEARCH_ENGINE_NAME"] == "petal"
    assert explicit_petal["WEB_SEARCH_API_KEY"] == "petal-key"
    assert explicit_petal["WEB_SEARCH_URL"] == (
        "https://maas.example/v1/ai-tools/web-search"
    )


def test_explicit_petal_snapshot_never_reads_bound_tenant_overlay():
    from jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager import (
        load_deepresearch_config,
    )

    tenant_a_url = "https://tenant-a.example/search?token=tenant-a-secret"
    snapshot_b_url = "https://tenant-b.example/search?token=snapshot-b-token"
    snapshot_b = {
        "MODEL_NAME": "snapshot-model",
        "MODEL_PROVIDER": "openai",
        "API_BASE": "https://snapshot-b.example/v2",
        "API_KEY": "snapshot-model-key",
        "PETAL_SEARCH_URL": snapshot_b_url,
        "PETAL_SEARCH_HEADERS": '{"Authorization":"snapshot-b-header"}',
    }
    token = bind_task_env_overlay(
        {
            "PETAL_SEARCH_URL": tenant_a_url,
            "PETAL_SEARCH_HEADERS": '{"Authorization":"tenant-a-header"}',
        }
    )
    try:
        detected = DeepResearchTaskManager._detect_configured_search_engines(snapshot_b)
        loaded = load_deepresearch_config(snapshot_b)
    finally:
        reset_task_env_overlay(token)

    assert detected == {"petal": snapshot_b_url}
    assert loaded["WEB_SEARCH_ENGINE_NAME"] == "petal"
    assert loaded["WEB_SEARCH_API_KEY"] == snapshot_b_url
    assert tenant_a_url not in repr((detected, loaded))


def test_load_config_translates_model_gear_to_concrete_model():
    """档位关键字只能到隔离子进程边界为止，必须翻成具体模型 id。

    DeepResearch 子进程走 env 通道（relay OFFICE_CLAW_EFFECTIVE_MODEL → MODEL_NAME），
    没有 rail 也没有 request params。裸档位关键字直送 MaaS 会得到
    ``ModelArts.81009 Invalid model``（211800/181001 那串报错的根因）。
    """
    from jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager import (
        load_deepresearch_config,
    )

    base = {"API_BASE": "https://maas.example/v2/", "API_KEY": "k"}

    assert load_deepresearch_config({**base, "MODEL_NAME": "extreme"})["LLM_MODEL_NAME"] == "glm-5.2"
    for gear in ("fast", "balanced", "auto"):
        assert load_deepresearch_config({**base, "MODEL_NAME": gear})["LLM_MODEL_NAME"] == "deepseek-v4.1-flash"
    # 显式 LLM_MODEL_NAME 优先，但同样要过表（同一通道，同一风险）。
    assert load_deepresearch_config({**base, "LLM_MODEL_NAME": "extreme", "MODEL_NAME": "glm-5.2"})[
        "LLM_MODEL_NAME"
    ] == "glm-5.2"

    # 具体模型名原样透传，不能被误翻。
    assert load_deepresearch_config({**base, "MODEL_NAME": "glm-5.2"})["LLM_MODEL_NAME"] == "glm-5.2"
    assert load_deepresearch_config({**base, "MODEL_NAME": "deepseek-v4.1-flash"})["LLM_MODEL_NAME"] == (
        "deepseek-v4.1-flash"
    )



@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"sections":[{"title":"背景"},{"name":"趋势"}]}',
            {"1": "背景", "2": "趋势"},
        ),
        (
            "# 深度研究报告：主题\n## 第1章：背景\n### 2. 趋势\n## 结论",
            {"1": "背景", "2": "趋势", "3": "结论"},
        ),
        (
            "# 大纲：用户需求洞察报告\n"
            "## 页面规划\n"
            "### P1: 用户画像构建（重点）\n"
            "### P2: 行为习惯分析（重点）\n"
            "### P3: 真实痛点挖掘（重点）\n"
            "### P4: 消费决策逻辑剖析（重点）\n"
            "### P5: 潜在需求识别与产品设计建议（重点）",
            {
                "1": "用户画像构建（重点）",
                "2": "行为习惯分析（重点）",
                "3": "真实痛点挖掘（重点）",
                "4": "消费决策逻辑剖析（重点）",
                "5": "潜在需求识别与产品设计建议（重点）",
            },
        ),
    ],
)
def test_extract_deepresearch_section_titles(text, expected):
    from jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager import (
        extract_deepresearch_section_titles,
    )

    assert extract_deepresearch_section_titles(text) == expected
