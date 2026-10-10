"""Lifecycle contracts for producer-owning Auto-Harness streams."""

from __future__ import annotations

# TEST ONLY: URL fixtures use the RFC-reserved .test domain and remain inside
# mocked model/service objects; no external request is performed.

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.auto_harness import service as service_module
from jiuwenswarm.agents.harness.common.auto_harness.service import (
    ActiveAutoHarnessRun,
    AutoHarnessService,
)
from jiuwenswarm.common.schema.agent import AgentResponseChunk


def _bare_service() -> AutoHarnessService:
    service = object.__new__(AutoHarnessService)
    service._active_runs = {}
    service._base_config = SimpleNamespace(repo_url="https://example.test/repo.git")
    service._agent = None
    service._stream_event_rail = None
    return service


def _active_run(
    task: asyncio.Task[Any],
    *,
    orchestrator: Any = None,
) -> ActiveAutoHarnessRun:
    return ActiveAutoHarnessRun(
        session_id="session",
        request_id="request",
        repo_url="https://example.test/repo.git",
        local_repo=Path("/tmp/repo"),
        task=task,
        orchestrator=orchestrator,
        pipeline_preference="test",
    )


class _CancelRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.cancel_calls = 0
        self.fail = fail
        self.agent = None

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self.fail:
            raise RuntimeError("cancel failed")


@pytest.mark.asyncio
async def test_settle_owned_run_cancels_producer_when_orchestrator_cancel_fails() -> (
    None
):
    service = _bare_service()
    producer_started = asyncio.Event()
    producer_stopped = asyncio.Event()

    async def producer() -> None:
        producer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            producer_stopped.set()

    producer_task = asyncio.create_task(producer())
    await producer_started.wait()
    orchestrator = _CancelRecorder(fail=True)
    active_run = _active_run(producer_task, orchestrator=orchestrator)
    service._active_runs["session"] = active_run

    await service._settle_owned_run(
        session_id="session",
        producer_task=producer_task,
        active_run=active_run,
        orchestrator=orchestrator,
        cancel=True,
    )
    service._remove_owned_run(session_id="session", active_run=active_run)

    assert orchestrator.cancel_calls == 1
    assert producer_task.done()
    assert producer_stopped.is_set()
    assert "session" not in service._active_runs


@pytest.mark.asyncio
async def test_settle_owned_run_absorbs_producer_completion_error() -> None:
    service = _bare_service()

    async def producer() -> None:
        raise RuntimeError("producer failed")

    producer_task = asyncio.create_task(producer())
    await service._settle_owned_run(
        session_id="session",
        producer_task=producer_task,
        active_run=None,
        orchestrator=None,
        cancel=False,
    )

    assert producer_task.done()
    assert isinstance(producer_task.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_settle_owned_run_reports_repeated_cancellation_after_cleanup() -> None:
    service = _bare_service()
    producer_started = asyncio.Event()
    first_cancel_seen = asyncio.Event()
    release_producer = asyncio.Event()

    async def producer() -> None:
        producer_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancel_seen.set()
            await release_producer.wait()

    producer_task = asyncio.create_task(producer())
    await producer_started.wait()
    active_run = _active_run(producer_task)
    cleanup_task = asyncio.create_task(
        service._settle_owned_run(
            session_id="session",
            producer_task=producer_task,
            active_run=active_run,
            orchestrator=None,
            cancel=True,
        )
    )

    await first_cancel_seen.wait()
    cleanup_task.cancel()
    await asyncio.sleep(0)
    cleanup_task.cancel()
    await asyncio.sleep(0)
    assert not cleanup_task.done()

    release_producer.set()
    caller_cancelled = await asyncio.wait_for(cleanup_task, timeout=1)

    assert producer_task.done()
    assert caller_cancelled is True


@pytest.mark.asyncio
async def test_remove_owned_run_preserves_replacement_mapping() -> None:
    service = _bare_service()

    async def complete() -> None:
        return None

    old_task = asyncio.create_task(complete())
    replacement_task = asyncio.create_task(complete())
    await asyncio.gather(old_task, replacement_task)
    old_run = _active_run(old_task)
    replacement_run = _active_run(replacement_task)
    service._active_runs["session"] = replacement_run

    service._remove_owned_run(session_id="session", active_run=old_run)

    assert service._active_runs["session"] is replacement_run


class _BlockingOrchestrator(_CancelRecorder):
    def __init__(
        self,
        *,
        producer_started: asyncio.Event,
        producer_stopped: asyncio.Event,
    ) -> None:
        super().__init__()
        self.producer_started = producer_started
        self.producer_stopped = producer_stopped
        self.artifacts = SimpleNamespace(put=lambda *_args, **_kwargs: None)

    async def run_session_stream(self, **_kwargs: Any):
        self.producer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.producer_stopped.set()
        if False:
            yield None


async def _clone_repo(*_args: Any, **_kwargs: Any) -> Path:
    return Path("/tmp/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["activate", "implement", "full"])
async def test_owned_stream_aclose_finishes_producer_before_removing_run(
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    service = _bare_service()
    producer_started = asyncio.Event()
    producer_stopped = asyncio.Event()
    orchestrator = _BlockingOrchestrator(
        producer_started=producer_started,
        producer_stopped=producer_stopped,
    )
    config = SimpleNamespace(
        pipeline_preference="test",
        max_tasks_per_session=1,
        git_base_branch="develop",
    )
    request = SimpleNamespace(channel_id="web", params={})

    monkeypatch.setattr(
        service_module,
        "create_auto_harness_orchestrator",
        lambda *_args, **_kwargs: orchestrator,
    )
    monkeypatch.setattr(
        service,
        "build_auto_harness_config",
        lambda *_args, **_kwargs: config,
    )

    async def consume_stream(
        active_run: ActiveAutoHarnessRun, *_args: Any, **_kwargs: Any
    ):
        await producer_started.wait()
        yield (
            AgentResponseChunk(
                request_id="request",
                channel_id="web",
                payload={"event_type": "test.chunk"},
                is_complete=False,
            ),
            False,
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_consume_stream", consume_stream)

    if variant == "activate":
        runtime_path = Path("/tmp/runtime")
        monkeypatch.setattr(
            service,
            "_resolve_activate_only_runtime_path",
            lambda *_args: runtime_path,
        )
        monkeypatch.setattr(
            service,
            "_resolve_local_repo_for_debug",
            lambda: Path("/tmp/repo"),
        )

        class BlockingActivateStage:
            async def stream(self, _ctx: Any):
                producer_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    producer_stopped.set()
                if False:
                    yield None

        monkeypatch.setattr(
            service_module,
            "ExtendActivateStage",
            BlockingActivateStage,
        )
        stream = service.run_activate_only(
            request,
            "session",
            "request",
            "activate-only",
        )
    elif variant == "implement":
        monkeypatch.setattr(
            service,
            "_resolve_implement_only_design_path",
            lambda *_args: Path("/tmp/design.json"),
        )
        monkeypatch.setattr(
            service, "_load_extension_designs", lambda _path: [object()]
        )
        monkeypatch.setattr(service, "clone_or_update_repo", _clone_repo)

        async def blocking_implementation_stream(*_args: Any, **_kwargs: Any):
            producer_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                producer_stopped.set()
            if False:
                yield None

        monkeypatch.setattr(
            service_module.ExtensionTaskPipeline,
            "run_isolated_stream",
            blocking_implementation_stream,
        )
        stream = service.run_implement_only(
            request,
            "session",
            "request",
            "implement-only",
        )
    else:
        monkeypatch.setattr(service, "clone_or_update_repo", _clone_repo)
        stream = service.run(
            request,
            "session",
            "request",
            query="test",
        )

    processing_chunk = await anext(stream)
    streamed_chunk = await anext(stream)
    active_run = service._active_runs["session"]

    assert processing_chunk.payload["is_processing"] is True
    assert streamed_chunk.payload == {"event_type": "test.chunk"}
    assert active_run.task.done() is False

    await stream.aclose()

    assert orchestrator.cancel_calls == 1
    assert active_run.task.done()
    assert producer_stopped.is_set()
    assert "session" not in service._active_runs


@pytest.mark.asyncio
async def test_normal_settlement_cancellation_cleans_up_then_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _bare_service()
    producer_started = asyncio.Event()
    producer_stopped = asyncio.Event()
    orchestrator = _BlockingOrchestrator(
        producer_started=producer_started,
        producer_stopped=producer_stopped,
    )
    config = SimpleNamespace(
        pipeline_preference="test",
        max_tasks_per_session=1,
        git_base_branch="develop",
    )
    request = SimpleNamespace(channel_id="web", params={})

    monkeypatch.setattr(
        service_module,
        "create_auto_harness_orchestrator",
        lambda *_args, **_kwargs: orchestrator,
    )
    monkeypatch.setattr(
        service,
        "build_auto_harness_config",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(service, "clone_or_update_repo", _clone_repo)

    async def consume_stream(
        _active_run: ActiveAutoHarnessRun,
        *_args: Any,
        **_kwargs: Any,
    ):
        await producer_started.wait()
        if False:
            yield None

    monkeypatch.setattr(service, "_consume_stream", consume_stream)
    stream = service.run(
        request,
        "session",
        "request",
        query="test",
    )
    processing_chunk = await anext(stream)
    emitted_after_processing: list[AgentResponseChunk] = []

    async def collect_remaining() -> None:
        async for chunk in stream:
            emitted_after_processing.append(chunk)

    collector_task = asyncio.create_task(collect_remaining())
    await producer_started.wait()
    await asyncio.sleep(0)
    collector_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(collector_task, timeout=1)

    assert processing_chunk.payload["is_processing"] is True
    assert emitted_after_processing == []
    assert orchestrator.cancel_calls == 1
    assert producer_stopped.is_set()
    assert "session" not in service._active_runs


@pytest.mark.asyncio
async def test_package_refresh_cancellation_removes_run_without_terminal_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _bare_service()
    producer_finished = asyncio.Event()
    refresh_started = asyncio.Event()
    orchestrator = _CancelRecorder()
    orchestrator.agent = None
    config = SimpleNamespace(
        pipeline_preference=service_module.EXTENDED_EVOLVE_PIPELINE,
        max_tasks_per_session=1,
        git_base_branch="develop",
    )
    request = SimpleNamespace(channel_id="web", params={})

    async def completed_stream(**_kwargs: Any):
        producer_finished.set()
        if False:
            yield None

    orchestrator.run_session_stream = completed_stream
    monkeypatch.setattr(
        service_module,
        "create_auto_harness_orchestrator",
        lambda *_args, **_kwargs: orchestrator,
    )
    monkeypatch.setattr(
        service,
        "build_auto_harness_config",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(service, "clone_or_update_repo", _clone_repo)

    async def consume_stream(
        active_run: ActiveAutoHarnessRun,
        *_args: Any,
        **_kwargs: Any,
    ):
        await active_run.task
        if False:
            yield None

    async def blocked_to_thread(_func: Any, *_args: Any, **_kwargs: Any) -> Any:
        refresh_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_consume_stream", consume_stream)
    monkeypatch.setattr(service_module.asyncio, "to_thread", blocked_to_thread)
    stream = service.run(
        request,
        "session",
        "request",
        query="test",
    )
    processing_chunk = await anext(stream)
    emitted_after_processing: list[AgentResponseChunk] = []

    async def collect_remaining() -> None:
        async for chunk in stream:
            emitted_after_processing.append(chunk)

    collector_task = asyncio.create_task(collect_remaining())
    await producer_finished.wait()
    await refresh_started.wait()
    collector_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(collector_task, timeout=1)

    assert processing_chunk.payload["is_processing"] is True
    assert emitted_after_processing == []
    assert "session" not in service._active_runs
