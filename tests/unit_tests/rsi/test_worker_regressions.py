# -*- coding: utf-8 -*-
"""RSI PR !5798 检视修复回归测试（worker 队列可靠性 / 推送调度 / 投影契约 / store 锁）。

覆盖：F1 enqueue 运行中入队、F4 QUEUED 幂等、F2 _run_loop 容错、
F3 merge_results 锁内落盘、F5 async send_push 调度、F7 derive_progress 404、
F9 树拓扑 parent 解析、F10 畸形载荷防御、F11 rmtree 失败传播、F12 公开回调 setter。
"""
import asyncio
import contextlib
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiNotReady,
    RsiTaskNotFound,
    RsiTaskStateConflict,
)
from jiuwenswarm.agents.harness.common.rsi.models import TaskStatus
from jiuwenswarm.server.rsi import RsiAgentServerHandlers


@pytest.fixture
def ctx():
    with tempfile.TemporaryDirectory() as tmp:
        yield build_rsi_service_context(Path(tmp))


def _create(ctx, name="t"):
    return ctx.task_service.create({
        "scenario": "HARNESS",
        "name": name,
        "input_file": "C:/d.json",
        "model_refs": {"optimizer": "o", "tester": "e"},
    })["task_id"]


class _ControlAdapter:
    supports_pause = True
    supports_resume = True

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.pause_started = asyncio.Event()
        self.pause_release = asyncio.Event()
        self.terminate_started = asyncio.Event()
        self.terminate_release = asyncio.Event()

    async def pause(self, task_id: str):
        self.calls.append(f"pause:{task_id}")
        self.pause_started.set()
        await self.pause_release.wait()
        return SimpleNamespace(status="PAUSED")

    async def terminate(self, task_id: str):
        self.calls.append(f"terminate:{task_id}")
        self.terminate_started.set()
        await self.terminate_release.wait()
        return SimpleNamespace(status="TERMINATED")


def _mark_running(ctx, task_id: str) -> None:
    ctx.store.update_status(task_id, ["CREATED"], "QUEUED", cause="test")
    ctx.store.update_status(task_id, ["QUEUED"], "RUNNING", cause="test")


class TestWorkerEnqueue:
    async def test_enqueue_while_running_still_queues(self, ctx, monkeypatch):
        """F1：运行中分支必须入队（PR !5798 #1/#60），只改状态会导致任务永久卡 QUEUED。"""
        monkeypatch.setattr(ctx.worker, "_ensure_runner", lambda: None)
        t1 = _create(ctx, "t1")
        t2 = _create(ctx, "t2")
        ctx.store.update_status(t1, ["CREATED"], "QUEUED", cause="test")
        ctx.store.update_status(t1, ["QUEUED"], "RUNNING", cause="test")
        gate = asyncio.Event()
        blocker = asyncio.create_task(gate.wait())
        ctx.worker._running_task_id = t1
        ctx.worker._run_task = blocker
        try:
            status = ctx.worker.enqueue(t2)
            assert status == TaskStatus.QUEUED.value
            assert ctx.worker._queue.qsize() == 1
            assert ctx.store.get(t2).status == "QUEUED"
        finally:
            blocker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await blocker

    def test_enqueue_queued_task_idempotent(self, ctx, monkeypatch):
        """F4：QUEUED 任务重复 enqueue 不抛错、不重复入队（避免 QUEUED→QUEUED 自冲突）。"""
        monkeypatch.setattr(ctx.worker, "_ensure_runner", lambda: None)
        t = _create(ctx)
        assert ctx.worker.enqueue(t) == TaskStatus.QUEUED.value
        assert ctx.worker.enqueue(t) == TaskStatus.QUEUED.value
        assert ctx.worker._queue.qsize() == 1
        assert ctx.store.get(t).status == "QUEUED"

    def test_enqueue_paused_rejected(self, ctx, monkeypatch):
        monkeypatch.setattr(ctx.worker, "_ensure_runner", lambda: None)
        t = _create(ctx)
        ctx.store.update_status(t, ["CREATED"], "QUEUED", cause="t")
        ctx.store.update_status(t, ["QUEUED"], "PAUSED", cause="t")
        with pytest.raises(RsiTaskStateConflict):
            ctx.worker.enqueue(t)


class TestWorkerRunLoop:
    async def test_run_loop_survives_state_conflict(self, ctx, monkeypatch):
        """F2：单个任务状态冲突不拖垮 worker；finally 清理 _running_task_id + task_done。"""
        t = _create(ctx)
        original = ctx.store.update_status

        def _conflicting(task_id, from_states, to_state, cause=""):
            if cause == "worker.start":
                raise RsiTaskStateConflict(f"conflict {task_id}")
            return original(task_id, from_states, to_state, cause)

        monkeypatch.setattr(ctx.store, "update_status", _conflicting)
        ctx.store.update_status(t, ["CREATED"], "QUEUED", cause="enqueue")
        ctx.worker._queue.put_nowait(t)
        ctx.worker._last_enqueued = t
        runner = asyncio.create_task(ctx.worker._run_loop())
        await asyncio.sleep(0.05)
        assert not runner.done()
        assert ctx.worker._running_task_id is None
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    async def test_persist_results_delegates_to_store_merge(self, ctx, monkeypatch):
        """F3：_persist_results 委托 store.merge_results（锁内读-改-写），不再直接写 task.json。"""
        t = _create(ctx)
        captured: dict = {}

        def fake_merge(task_id, results):
            captured["task_id"] = task_id
            captured["results"] = dict(results)

        monkeypatch.setattr(ctx.store, "merge_results", fake_merge)

        class FakeResult:
            best_score = 0.8
            state_path = "/s"
            report_path = None  # None 应跳过

        ctx.worker._persist_results(t, FakeResult())
        assert captured["task_id"] == t
        assert captured["results"] == {"best_score": 0.8, "state_path": "/s"}

    async def test_provider_polling_timeout_returns_failure(self, ctx):
        """A Provider stuck in RUNNING must release the worker with a failure result."""

        class StuckAdapter:
            def __init__(self) -> None:
                self.terminate_calls: list[str] = []

            def read_state(self, task_id: str):
                del task_id
                return SimpleNamespace(status="running")

            async def terminate(self, task_id: str):
                self.terminate_calls.append(task_id)
                return SimpleNamespace(status="TERMINATED")

        adapter = StuckAdapter()
        ctx.worker.provider_poll_timeout = 0.02
        result = await ctx.worker._wait_for_provider_terminal(  # noqa: SLF001
            "rsi-stuck",
            adapter,
            SimpleNamespace(status="running"),
        )

        assert result.status == "failed"
        assert result.error_code == "PROVIDER_TIMEOUT"
        assert result.error_message == "Provider 超时未进入终态"
        assert adapter.terminate_calls == ["rsi-stuck"]

    def test_paper_provider_polling_has_no_iteration_wall_clock_cap(self, ctx):
        """Paper runs must not be killed by a guessed per-iteration budget."""
        paper = SimpleNamespace(artifact_type="PAPER", max_iterations=3)
        program = SimpleNamespace(artifact_type="PROGRAM", max_iterations=3)

        assert ctx.worker._provider_poll_timeout_for(paper) is None  # noqa: SLF001
        assert ctx.worker._provider_poll_timeout_for(program) == ctx.worker.provider_poll_timeout  # noqa: SLF001

    async def test_paper_provider_waits_for_terminal_state_without_total_timeout(self, ctx):
        """A long-running paper Provider is allowed to finish normally."""

        class EventuallyCompleteAdapter:
            def __init__(self) -> None:
                self.reads = 0

            def read_state(self, task_id: str):
                del task_id
                self.reads += 1
                status = "completed" if self.reads >= 2 else "running"
                return SimpleNamespace(status=status, best_node_id="node-1")

        adapter = EventuallyCompleteAdapter()
        result = await ctx.worker._wait_for_provider_terminal(  # noqa: SLF001
            "rsi-paper-long-run",
            adapter,
            SimpleNamespace(status="running"),
            timeout=None,
        )

        assert result.status == "completed"
        assert adapter.reads == 2

    async def test_provider_timeout_does_not_block_next_queued_task(self, ctx):
        """Timeout terminalization lets the following queued task start."""

        class StuckAdapter:
            def __init__(self) -> None:
                self.run_tasks: list[str] = []

            def build_request(self, task_view, *, resume: bool = False):
                del resume
                return task_view

            async def run(self, request, *, on_event=None):
                del on_event
                self.run_tasks.append(request.task_id)
                return SimpleNamespace(status="running")

            def read_state(self, task_id: str):
                del task_id
                return SimpleNamespace(status="running")

        adapter = StuckAdapter()
        ctx.register_adapters({"HARNESS": adapter})
        ctx.worker.provider_poll_timeout = 0.02
        first = _create(ctx, "stuck-1")
        second = _create(ctx, "stuck-2")

        ctx.worker.enqueue(first)
        ctx.worker.enqueue(second)
        await asyncio.wait_for(ctx.worker._queue.join(), timeout=1)  # noqa: SLF001

        assert adapter.run_tasks == [first, second]
        assert ctx.store.get(first).status == TaskStatus.FAILED.value
        assert ctx.store.get(second).status == TaskStatus.FAILED.value
        runner = ctx.worker._run_task
        assert runner is not None
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


class TestProviderControl:
    async def test_pause_commits_state_after_provider_returns(self, ctx):
        adapter = _ControlAdapter()
        ctx.register_adapters({"HARNESS": adapter})
        task_id = _create(ctx)
        _mark_running(ctx, task_id)

        assert ctx.worker.cancel(task_id, "pause") == TaskStatus.RUNNING.value
        assert ctx.store.get(task_id).status == TaskStatus.RUNNING.value
        control = ctx.worker._control_tasks[task_id]  # noqa: SLF001 - inspect scheduled control

        adapter.pause_release.set()
        await control

        assert ctx.store.get(task_id).status == TaskStatus.PAUSED.value

    async def test_pause_frees_the_queue_before_the_provider_confirms(self, ctx):
        """暂停一提出，排在后面的任务就该开始，而不是等收尾做完。

        pause 不会让引擎就地停下：Provider 先把在飞的那次扩展做完（一次模型调用
        加一次评测）。实测一次真实运行里这段花了 2 分 34 秒，期间执行位一直被占，
        第二个任务在队列里干等——等的是一件谁都不再要其结果的工作。
        """

        class BlockingAdapter:
            supports_pause = True
            supports_resume = True

            def __init__(self) -> None:
                self.started: list[str] = []
                self.release = asyncio.Event()

            def build_request(self, task_view, *, resume: bool = False):
                del resume
                return task_view

            async def run(self, request, *, on_event=None):
                del on_event
                self.started.append(request.task_id)
                await self.release.wait()
                return SimpleNamespace(status="completed")

            def read_state(self, task_id: str):
                del task_id
                return SimpleNamespace(status="completed")

            async def pause(self, task_id: str):
                del task_id
                return SimpleNamespace(status="PAUSED")

        adapter = BlockingAdapter()
        ctx.register_adapters({"HARNESS": adapter})
        first = _create(ctx, "pausing")
        second = _create(ctx, "waiting")

        ctx.worker.enqueue(first)
        ctx.worker.enqueue(second)
        for _ in range(100):                      # 第一个进入 run 并停住
            await asyncio.sleep(0)
            if adapter.started:
                break
        assert adapter.started == [first]

        assert ctx.worker.cancel(first, "pause") == TaskStatus.RUNNING.value
        for _ in range(100):                      # 收尾还没结束，第二个已经开跑
            await asyncio.sleep(0)
            if len(adapter.started) > 1:
                break

        assert adapter.started == [first, second], "暂停中的任务仍占着执行位"
        assert not adapter.release.is_set(), "第一个任务其实还没跑完"
        assert ctx.store.get(second).status == TaskStatus.RUNNING.value

        adapter.release.set()
        await asyncio.wait_for(ctx.worker._queue.join(), timeout=1)  # noqa: SLF001
        runner = ctx.worker._run_task                                # noqa: SLF001
        assert runner is not None
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    async def test_failed_provider_control_keeps_public_state(self, ctx):
        class FailingAdapter(_ControlAdapter):
            async def pause(self, task_id: str):
                self.calls.append(f"pause:{task_id}")
                raise RuntimeError("provider pause failed")

        adapter = FailingAdapter()
        ctx.register_adapters({"HARNESS": adapter})
        task_id = _create(ctx)
        _mark_running(ctx, task_id)

        assert ctx.worker.cancel(task_id, "pause") == TaskStatus.RUNNING.value
        control = ctx.worker._control_tasks[task_id]  # noqa: SLF001 - inspect scheduled control
        with pytest.raises(RuntimeError, match="provider pause failed"):
            await control

        assert ctx.store.get(task_id).status == TaskStatus.RUNNING.value

    async def test_controls_are_serialized_instead_of_dropped(self, ctx):
        adapter = _ControlAdapter()
        ctx.register_adapters({"HARNESS": adapter})
        task_id = _create(ctx)
        _mark_running(ctx, task_id)

        assert ctx.worker.cancel(task_id, "pause") == TaskStatus.RUNNING.value
        assert ctx.worker.cancel(task_id, "terminate") == TaskStatus.RUNNING.value
        control = ctx.worker._control_tasks[task_id]  # noqa: SLF001 - inspect scheduled control

        await asyncio.wait_for(adapter.pause_started.wait(), timeout=1)
        assert adapter.calls == [f"pause:{task_id}"]
        assert ctx.store.get(task_id).status == TaskStatus.RUNNING.value

        adapter.pause_release.set()
        await asyncio.wait_for(adapter.terminate_started.wait(), timeout=1)
        assert adapter.calls == [f"pause:{task_id}", f"terminate:{task_id}"]
        assert ctx.store.get(task_id).status == TaskStatus.PAUSED.value

        adapter.terminate_release.set()
        await control
        assert ctx.store.get(task_id).status == TaskStatus.TERMINATED.value

    async def test_terminate_notifies_provider_for_paused_artifact(self, ctx, tmp_path: Path):
        adapter = _ControlAdapter()
        artifact_path = tmp_path / "program"
        artifact_path.mkdir()
        task_id = ctx.task_service.create({
            "scenario": "ARTIFACT",
            "artifact_type": "PROGRAM",
            "name": "paused-program",
            "artifact_path": str(artifact_path),
            "model_refs": {"optimizer": "mock"},
        })["task_id"]
        ctx.register_adapters({"ARTIFACT:PROGRAM": adapter})
        _mark_running(ctx, task_id)
        ctx.store.update_status(task_id, ["RUNNING"], "PAUSED", cause="test")

        assert ctx.worker.cancel(task_id, "terminate") == TaskStatus.PAUSED.value
        control = ctx.worker._control_tasks[task_id]  # noqa: SLF001 - inspect scheduled control
        await asyncio.wait_for(adapter.terminate_started.wait(), timeout=1)
        assert adapter.calls == [f"terminate:{task_id}"]
        assert ctx.store.get(task_id).status == TaskStatus.PAUSED.value

        adapter.terminate_release.set()
        await control
        assert ctx.store.get(task_id).status == TaskStatus.TERMINATED.value

    def test_provider_control_requires_running_loop(self, ctx):
        adapter = _ControlAdapter()
        ctx.register_adapters({"HARNESS": adapter})
        task_id = _create(ctx)
        _mark_running(ctx, task_id)

        with pytest.raises(RsiNotReady):
            ctx.worker.cancel(task_id, "pause")
        assert ctx.store.get(task_id).status == TaskStatus.RUNNING.value


class TestStoreMerge:
    def test_merge_results_lock_protected(self, ctx):
        t = _create(ctx)
        ctx.store.merge_results(t, {"best_score": 0.9, "state_path": "x"})
        task = ctx.store.get(t)
        assert task.config["results"]["best_score"] == 0.9
        assert task.config["results"]["state_path"] == "x"
        # None 值跳过，保留既有值
        ctx.store.merge_results(t, {"best_score": None, "report_path": "r"})
        task = ctx.store.get(t)
        assert task.config["results"]["best_score"] == 0.9
        assert task.config["results"]["report_path"] == "r"


class TestPushScheduling:
    async def test_push_schedules_async_send(self, ctx):
        """F5：async send_push 被调度执行，而非静默丢弃（PR !5798 #5/#59）。"""
        sent = []

        async def async_send(msg):
            sent.append(msg)

        h = RsiAgentServerHandlers(ctx, send_push=async_send, harness_refs_provider=lambda: None)
        h._push("rsi.training.progress", {"task_id": "t"})
        await asyncio.sleep(0.02)
        assert len(sent) == 1
        assert sent[0]["payload"]["event_type"] == "rsi.training.progress"
        assert sent[0]["payload"]["task_id"] == "t"

    def test_push_sync_send_unchanged(self, ctx):
        sent = []

        def sync_send(msg):
            sent.append(msg)
            return True

        h = RsiAgentServerHandlers(ctx, send_push=sync_send, harness_refs_provider=lambda: None)
        h._push("rsi.training.status.changed", {"task_id": "t"})
        assert len(sent) == 1
        assert sent[0]["payload"]["event_type"] == "rsi.training.status.changed"


class TestProjectorContract:
    def test_derive_progress_unknown_task_raises(self, ctx):
        """F7：真未知任务（无节点也无 metric）抛 404，不再静默返回零值进度。"""
        with pytest.raises(RsiTaskNotFound):
            ctx.projector.derive_progress("rsi-ghost")

    def test_derive_progress_metric_only_ok(self, ctx):
        """P2 链路先收到 metric 事件（root 尚未注册）仍返回进度，不误抛 404。"""
        ctx.projector.on_progress_metric("rsi-t1", {"iteration": 1})
        p = ctx.projector.derive_progress("rsi-t1")
        assert p["iteration"] == 1

    def test_derive_progress_malformed_metric_safe(self, ctx):
        """F10：畸形 metric 载荷不抛 ValueError/TypeError。"""
        ctx.projector.register_root("rsi-t1")
        ctx.projector.on_progress_metric("rsi-t1", {"iteration": "abc", "total_iterations": None})
        p = ctx.projector.derive_progress("rsi-t1")
        assert p["iteration"] == 0
        assert p["total_iterations"] == 0

    def test_node_parent_ref_resolves_depth(self, ctx):
        """F9：非 root 父节点经 _ref_index 反查，树深度 >1（PR !5798 #9）。"""
        ctx.projector.register_root("rsi-t1")
        ctx.projector.on_node_created("rsi-t1", {
            "node": {"ref": "c1", "parent_ref": "root", "outcome": "ADOPTED", "accepted": True},
        })
        child = ctx.projector.on_node_created("rsi-t1", {
            "node": {"ref": "c2", "parent_ref": "c1", "outcome": "REJECTED", "accepted": False},
        })
        assert child is not None
        assert child.parent_id == "N1"
        tree = ctx.projector.derive_tree("rsi-t1")
        assert tree["depth"] == 2


class TestDeleteFailures:
    def test_delete_propagates_rmtree_failure(self, ctx, monkeypatch):
        """F11：rmtree 失败不再静默吞掉（PR !5798 #11）。"""
        ctx.bind_task_service(harness_refs_provider=lambda: None)
        t = _create(ctx)
        import jiuwenswarm.agents.harness.common.rsi.task_store as ts

        def _boom(*args, **kwargs):
            raise PermissionError("locked")

        with monkeypatch.context() as patch:
            patch.setattr(ts.shutil, "rmtree", _boom)
            with pytest.raises(PermissionError):
                ctx.task_service.delete({"task_id": t})

    def test_delete_idempotent_when_dir_already_gone(self, ctx):
        """目录已消失视作幂等成功（guard 通过后 rmtree 抛 FileNotFoundError 不误报）。"""
        ctx.bind_task_service(harness_refs_provider=lambda: None)
        t = _create(ctx)
        ctx.store.mark_active_ref_released(t)
        task_dir = ctx.store.task_dir(ctx.store.tasks_root, t)
        assert task_dir.is_dir()
        # 制造“get 成功但目录已删”的窗口：替换 task.json 后立即删目录不可行，
        # 直接预删目录 → delete 的 get 会抛 NotFound；此分支无独立可测窗口。
        # 验证正常删除路径仍工作。
        assert ctx.task_service.delete({"task_id": t}) == {"ok": True}
        assert not task_dir.exists()


class TestStatusCallback:
    def test_public_setter_registers_callback(self, ctx):
        """F12：set_status_changed_callback 公开注入 P1 钩子（替代直接改私有属性）。"""
        seen = []
        ctx.store.set_status_changed_callback(lambda task_id, old, new: seen.append((task_id, old, new)))
        t = _create(ctx)
        ctx.store.update_status(t, ["CREATED"], "QUEUED", cause="x")
        assert (t, "CREATED", "QUEUED") in seen
