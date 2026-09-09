"""RsiWorker：单协程队列（并发=1）+ 事件链路装配（内部 v3 §4.2）。

- ``enqueue(task_id)``：队列空 → RUNNING 直接启动；有运行任务 → QUEUED（全局并发=1）。
- 启动时：``adapter.build_request(task)`` → 装配事件链路（sink→有界队列→消费协程）→
  ``await adapter.run(request, on_event=sink)``；正常返回 → COMPLETED；异常 → FAILED。
- 终态后 ``await q.join()`` 排空最后事件再关消费协程。
- ``cancel(mode)`` / ``resume()``：中优先级（I7/I8/I9）接口已落位 + 状态机校验，
  运行中的 artifact 任务会转发到 Provider 的 pause/terminate；
  ``resume(fingerprint_check)`` 仍由具体 Provider 负责断点指纹校验。
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiDatasetInvalid,
    RsiNotReady,
    RsiScenarioNotSupported,
    RsiTaskStateConflict,
)
from jiuwenswarm.agents.harness.common.rsi.event_consumer import RsiEventConsumer, consume_queue
from jiuwenswarm.agents.harness.common.rsi.events import EngineEvent
from jiuwenswarm.agents.harness.common.rsi.models import TaskStatus

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 128
_DEFAULT_POLL_TIMEOUT = object()
_PROVIDER_IN_PROGRESS = frozenset({"CREATED", "QUEUED", "RUNNING"})
# The generic Provider watchdog remains bounded, but PAPER is deliberately
# excluded from it below.  A paper iteration contains several model-backed
# modules and network retrieval; there is no reliable per-iteration wall-clock
# bound that can be multiplied by ``max_iterations`` without killing a healthy
# run.  PAPER is stopped by its Provider's explicit terminate path instead.
_PROVIDER_POLL_TIMEOUT_SECONDS = 30 * 60
_PROVIDER_POLL_INTERVAL_SECONDS = 0.1
_PROVIDER_HANDOFF_RETRY_SECONDS = 0.05
_PROVIDER_TERMINATE_TIMEOUT_SECONDS = 5.0


class RsiWorker:
    """全局单队列 worker（并发=1），跨场景共享。

    Args:
        store: RsiTaskStore
        adapters: scenario → RsiEngineAdapter（HARNESS/ARTIFACT）
        usage_recorder / projector / artifact_service: 事件链路消费方
        push_callbacks: ``{event_type: async (event_type, task_id, payload)->None}``
            （AgentServer 注入 send_push 包装）
    """

    def __init__(
        self,
        store: Any,
        adapters: dict[str, Any],
        usage_recorder: Any,
        projector: Any,
        artifact_service: Any,
        push_callbacks: dict[str, Any] | None = None,
        provider_poll_timeout: float = _PROVIDER_POLL_TIMEOUT_SECONDS,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.usage_recorder = usage_recorder
        self.projector = projector
        self.artifact_service = artifact_service
        self._push_callbacks = push_callbacks or {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._running_task_id: str | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._last_enqueued: str | None = None
        self._resume_task_ids: set[str] = set()
        self._control_tasks: dict[str, asyncio.Task[Any]] = {}
        self._execution_tasks: dict[str, asyncio.Task[Any]] = {}
        # 请求 pause/terminate 时用来提前让出执行位；见 ``_run_until_slot_free``。
        self._slot_released: dict[str, asyncio.Future[None]] = {}
        self._winding_down: set[asyncio.Task[Any]] = set()
        # Providers that return before their durable snapshot reaches a
        # terminal state are polled here.  Keep the bound configurable for
        # deployments and tests, while protecting the queue from a Provider
        # that is stuck in CREATED/QUEUED/RUNNING forever.
        self.provider_poll_timeout = max(0.1, float(provider_poll_timeout))

    # -- 队列 --

    def enqueue(self, task_id: str) -> str:
        """入队（内部 v3 §4.2 语义）。返回 QUEUED。

        - 单一路径：任何非 PAUSED / 非 QUEUED 任务统一 CREATED→QUEUED 并入队；
          ``_ensure_runner`` 在运行中时自动跳过（并发=1 语义）。
        - 已排队任务幂等短路，避免状态机 QUEUED→QUEUED 自冲突。
        """
        task = self.store.get(task_id)
        if task.status == TaskStatus.PAUSED.value:
            self._conflict(task_id, "PAUSED 任务请走 resume")
        if task.status == TaskStatus.QUEUED.value:
            self._ensure_runner()
            return TaskStatus.QUEUED.value
        self.store.update_status(
            task_id,
            [TaskStatus.CREATED.value],
            TaskStatus.QUEUED.value,
            cause="enqueue",
        )
        self._last_enqueued = task_id
        self._queue.put_nowait(task_id)
        self._ensure_runner()
        return TaskStatus.QUEUED.value

    # -- 取消 / 恢复（中优先级，接口落位） --

    def cancel(self, task_id: str, mode: str) -> str:
        """``pause`` / ``terminate`` and delegate running control to Provider."""
        mode = str(mode or "").lower()
        task = self.store.get(task_id)
        adapter = self._adapter_for(task.scenario, task.artifact_type)
        if mode == "pause":
            if task.scenario == "ARTIFACT" and (
                adapter is None or not bool(getattr(adapter, "supports_pause", False))
            ):
                raise RsiScenarioNotSupported("当前产物场景不支持 pause")
            if task.status not in {TaskStatus.RUNNING.value, TaskStatus.QUEUED.value}:
                self._conflict(task_id, f"状态 {task.status} 不可 pause")
            if task.status == TaskStatus.QUEUED.value:
                self._dequeue_locked(task_id)
                result = self.store.update_status(
                    task_id, [task.status], TaskStatus.PAUSED.value, cause=f"cancel({mode})"
                )
                return result.status
            # A running task remains RUNNING until the Provider confirms the
            # pause.  The control task owns the eventual state transition.
            self._schedule_provider_control(task_id, adapter, "pause")
            self._release_slot(task_id)
            return task.status
        if mode == "terminate":
            if task.status not in {
                TaskStatus.CREATED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.QUEUED.value,
                TaskStatus.PAUSED.value,
            }:
                self._conflict(task_id, f"状态 {task.status} 不可 terminate")
            if task.status == TaskStatus.QUEUED.value:
                self._dequeue_locked(task_id)
                result = self.store.update_status(
                    task_id, [task.status], TaskStatus.TERMINATED.value, cause=f"cancel({mode})"
                )
                return result.status
            if task.status in {TaskStatus.RUNNING.value, TaskStatus.PAUSED.value} and adapter is not None:
                if getattr(adapter, "supports_terminate", True):
                    # Keep the public state unchanged until the Provider confirms
                    # termination, including the PAUSED -> TERMINATED path.
                    self._schedule_provider_control(task_id, adapter, "terminate")
                    self._release_slot(task_id)
                    return task.status
                # Providers without a terminate hook (the production Harness
                # engine) are stopped by cancelling the running coroutine.
                exec_task = self._execution_tasks.get(task_id)
                if exec_task is not None and not exec_task.done():
                    exec_task.cancel()
                self._mark_terminated(task_id)
                return self.store.get(task_id).status
            result = self.store.update_status(
                task_id, [task.status], TaskStatus.TERMINATED.value, cause=f"cancel({mode})"
            )
            return result.status
        self._conflict(task_id, f"未知 mode: {mode}")

    def resume(self, task_id: str, fingerprint_check: bool = True) -> str:
        """``resume``：校验 + 入队，材料与引擎 fingerprint 由执行路径确认。"""
        task = self.store.get(task_id)
        if task.status != TaskStatus.PAUSED.value:
            self._conflict(task_id, "仅 PAUSED 可 resume")
        adapter = self._adapter_for(task.scenario, task.artifact_type)
        if task.scenario == "ARTIFACT" and (
            adapter is None or not bool(getattr(adapter, "supports_resume", False))
        ):
            raise RsiScenarioNotSupported("当前产物场景不支持 resume")
        if fingerprint_check:
            # HarnessProvider.resume 在真正调用引擎前校验任务材料；
            # openjiuwen 再按其持久化状态校验 engine fingerprint。
            # worker 只负责入队，避免在此处重复读取或伪造校验结果。
            logger.debug("[RSI] resume fingerprint 将由 Provider/引擎执行路径校验: task=%s", task_id)
        self.store.update_status(task_id, [TaskStatus.PAUSED.value], TaskStatus.QUEUED.value, cause="resume")
        self._resume_task_ids.add(task_id)
        self._last_enqueued = task_id
        self._queue.put_nowait(task_id)
        self._ensure_runner()
        return TaskStatus.QUEUED.value

    # -- 内部 --

    def _ensure_runner(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            return
        try:
            self._run_task = asyncio.create_task(self._run_loop())
        except RuntimeError:
            # 无运行中事件循环（同步上下文/单测/冷启动脚本）：入队成功但执行延后，
            # 由事件循环就绪后的下一次 enqueue 或显式 _ensure_runner 接管（内部 v3 §4.2）。
            logger.warning(
                "[RSI] 无运行中事件循环，任务已入队，等待事件循环接管: task=%s",
                getattr(self, "_last_enqueued", ""),
            )
            self._run_task = None

    async def _run_loop(self) -> None:
        while True:
            task_id = await self._queue.get()
            self._running_task_id = task_id
            try:
                self.store.update_status(
                    task_id,
                    [TaskStatus.QUEUED.value, TaskStatus.PAUSED.value],
                    TaskStatus.RUNNING.value,
                    cause="worker.start",
                )
                resume = task_id in self._resume_task_ids
                self._resume_task_ids.discard(task_id)
                exec_task = asyncio.create_task(self._run_until_slot_free(task_id, resume=resume))
                self._execution_tasks[task_id] = exec_task
                if self.store.get(task_id).status != TaskStatus.RUNNING.value:
                    # A terminate request won the race between RUNNING and
                    # task registration; cancel the fresh execution immediately.
                    exec_task.cancel()
                await exec_task
            except Exception:  # noqa: BLE001 - 单任务状态冲突/异常不拖垮 worker
                logger.exception("[RSI] 任务执行异常 task=%s，跳过继续取下一个", task_id)
            finally:
                self._execution_tasks.pop(task_id, None)
                self._running_task_id = None
                self._queue.task_done()

    async def _run_until_slot_free(self, task_id: str, *, resume: bool = False) -> None:
        """占住队列那唯一的执行位，直到运行结束——或者直到有人请求了 pause/terminate。

        pause 不会让引擎就地停下：Provider 要先把在飞的那次扩展做完，也就是一次
        模型调用加一次评测。实测一次真实运行里这段是 2 分 34 秒，而这段时间执行位
        一直被占着，排在后面的任务只能等一件谁都不再要其结果的工作做完。

        控制指令一发出就把执行位让出来，正在收尾的运行转到后台继续，队列接着走。
        代价是这段时间里两个运行短暂重叠，收尾的那个仍在占 CPU——所以以墙钟为
        指标的任务，那一次评测会偏慢一点。
        """
        runner = asyncio.create_task(self._execute_task(task_id, resume=resume))
        released: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._slot_released[task_id] = released
        try:
            await asyncio.wait({runner, released}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._slot_released.pop(task_id, None)
            released.cancel()
        if runner.done():
            await runner          # 异常照旧抛给 _run_loop 的处理分支
            return
        # 后台收尾：留住引用，否则事件循环可能把这个 Task 回收掉。
        self._winding_down.add(runner)
        runner.add_done_callback(self._winding_down.discard)
        logger.info("[RSI] 控制指令已下发，任务转后台收尾，队列继续: task=%s", task_id)

    def _release_slot(self, task_id: str) -> None:
        """让出执行位。控制指令已经在路上，等它落地不必占着队列。"""
        released = self._slot_released.get(task_id)
        if released is not None and not released.done():
            released.set_result(None)

    async def _execute_task(self, task_id: str, *, resume: bool = False) -> None:
        task_view = self.store.get_view(task_id)
        adapter = self._adapter_for(task_view.scenario, task_view.artifact_type)
        if adapter is None:
            self.store.update_status(
                task_id, [TaskStatus.RUNNING.value], TaskStatus.FAILED.value,
                cause=f"scenario adapter not registered: {task_view.scenario}",
            )
            return
        queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        consumer = RsiEventConsumer(
            task_id=task_id,
            usage_recorder=self.usage_recorder,
            projector=self.projector,
            artifact_service=self.artifact_service,
        )
        consumer.bind_push(
            on_progress=self._push("rsi.training.progress"),
            on_tree_delta=self._push("rsi.training.tree.delta"),
        )
        consume_task = asyncio.create_task(consume_queue(queue, consumer))
        result: Any = None
        cancelled = False
        try:
            if hasattr(adapter, "validate_input"):
                input_path = (
                    task_view.artifact_path or task_view.config.get("artifact_path")
                    if task_view.scenario == "ARTIFACT"
                    else task_view.input_file
                )
                validation = adapter.validate_input(
                    input_path,
                    scenario=task_view.scenario,
                    artifact_type=task_view.artifact_type,
                )
                if not bool(getattr(validation, "valid", False)):
                    errors = getattr(validation, "errors", None) or []
                    raise RsiDatasetInvalid("Provider 输入校验失败", errors=errors)
            request = adapter.build_request(task_view, resume=resume)
            if resume:
                result = await adapter.resume(request, on_event=_sink(queue))
            else:
                result = await adapter.run(request, on_event=_sink(queue))
            # PaperArtifactProviderImpl starts its orchestrator and returns a
            # current ``running`` result while the actual tree execution
            # continues in the same event loop.  Keep the worker task alive
            # until the durable Provider snapshot reaches a terminal state;
            # otherwise the generic result handler would mark it failed as
            # soon as it saw the initial running result.
            if _provider_status(result) in _PROVIDER_IN_PROGRESS:
                result = await self._wait_for_provider_terminal(
                    task_id,
                    adapter,
                    result,
                    timeout=self._provider_poll_timeout_for(task_view),
                )
            self._apply_result_status(task_id, result)
        except asyncio.CancelledError:
            cancelled = True
            logger.info("[RSI] 任务执行被终止 task=%s", task_id)
            self._mark_terminated(task_id)
            result = None
        except Exception as exc:  # noqa: BLE001
            logger.exception("[RSI] 任务执行失败 task=%s: %s", task_id, exc)
            self._mark_failed_if_running(task_id, str(exc)[:200])
        finally:
            try:
                if cancelled:
                    # Release the single-worker queue immediately.  Remaining
                    # engine events are discarded rather than draining them on
                    # the critical path.
                    queue.put_nowait(None)
                    consume_task.cancel()
                    try:
                        await consume_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001
                        logger.exception("[RSI] 事件消费协程退出异常 task=%s", task_id)
                else:
                    try:
                        await queue.join()
                    except Exception:  # noqa: BLE001
                        pass
                    queue.put_nowait(None)
                    try:
                        await consume_task
                    except Exception:  # noqa: BLE001
                        logger.exception("[RSI] 事件消费协程退出异常 task=%s", task_id)
                self._persist_results(task_id, result)
            except asyncio.CancelledError:
                # A late cancellation arriving during cleanup must not kill the
                # single-worker loop; the public state is already TERMINATED.
                logger.info("[RSI] 任务清理阶段被取消 task=%s", task_id)
                self._mark_terminated(task_id)

    def _mark_terminated(self, task_id: str) -> None:
        """将运行中的任务落到 TERMINATED（冲突时由其它控制路径持有终态）。"""
        try:
            current = self.store.get(task_id).status
            if current == TaskStatus.TERMINATED.value:
                return
            if current in {
                TaskStatus.CREATED.value,
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.PAUSED.value,
            }:
                self.store.update_status(
                    task_id,
                    [current],
                    TaskStatus.TERMINATED.value,
                    cause="worker.cancelled",
                )
        except RsiTaskStateConflict:
            logger.debug("[RSI] task state changed while marking terminated: %s", task_id)

    def _adapter_for(self, scenario: str | None, artifact_type: str | None) -> Any:
        scenario_key = str(scenario or "").strip().upper()
        artifact_key = str(artifact_type or "").strip().upper()
        candidates: list[str] = []
        if scenario_key == "ARTIFACT":
            if artifact_key:
                candidates.extend(
                    [
                        f"ARTIFACT:{artifact_key}",
                        f"artifact:{artifact_key.lower()}",
                    ]
                )
            candidates.append("ARTIFACT")
        elif scenario_key:
            candidates.extend([scenario_key, scenario_key.lower()])
        for key in candidates:
            adapter = self.adapters.get(key)
            if adapter is not None:
                return adapter
        return None

    def _provider_poll_timeout_for(self, task_view: Any) -> float | None:
        """Return the polling budget for a task.

        Harness and program Providers retain the generic watchdog.  Paper
        runs are intentionally unbounded here: their six model-backed modules
        include web retrieval and compilation, so deriving a total deadline
        from the number of tree iterations is only a guess and can terminate
        a healthy run.  The paper Provider exposes ``terminate`` for explicit
        user cancellation, and a terminal Provider snapshot still ends the
        polling loop immediately.
        """
        if str(getattr(task_view, "artifact_type", "")).upper() == "PAPER":
            return None
        return self.provider_poll_timeout

    def _apply_result_status(self, task_id: str, result: Any) -> None:
        status = _provider_status(result, default="COMPLETED")
        if status in _PROVIDER_IN_PROGRESS:
            # A Provider may legitimately return its current state from
            # ``run``.  The polling path above normally resolves it; keeping
            # this guard makes older/custom Providers fail-safe instead of
            # converting a non-terminal result into FAILED.
            if not getattr(result, "error_code", None):
                return
            status = "FAILED"
        target = {
            "COMPLETED": TaskStatus.COMPLETED.value,
            "FAILED": TaskStatus.FAILED.value,
            "PAUSED": TaskStatus.PAUSED.value,
            "TERMINATED": TaskStatus.TERMINATED.value,
        }.get(status)
        if target is None:
            target = TaskStatus.FAILED.value
        current = self.store.get(task_id).status
        if current != TaskStatus.RUNNING.value:
            return
        self.store.update_status(
            task_id,
            [TaskStatus.RUNNING.value],
            target,
            cause=f"provider.{status.lower()}",
        )

    async def _wait_for_provider_terminal(
        self,
        task_id: str,
        adapter: Any,
        initial_result: Any,
        *,
        timeout: float | None | object = _DEFAULT_POLL_TIMEOUT,
    ) -> Any:
        """Wait for Providers whose ``run`` returns before execution ends.

        A Provider crash or lost snapshot must not permanently occupy the
        single-worker queue.  The timeout result is deliberately shaped like
        an ``EngineResult`` so the normal task-state and result persistence
        paths mark the task failed and allow the next queued task to run.
        """
        read_state = getattr(adapter, "read_state", None)
        if not callable(read_state):
            logger.warning(
                "[RSI] Provider.run returned %s without read_state; task=%s remains running",
                _provider_status(initial_result),
                task_id,
            )
            return initial_result

        if timeout is _DEFAULT_POLL_TIMEOUT:
            poll_timeout: float | None = self.provider_poll_timeout
        elif timeout is None:
            poll_timeout = None
        else:
            poll_timeout = max(0.1, float(timeout))
        deadline = time.monotonic() + poll_timeout if poll_timeout is not None else None
        last_status = _provider_status(initial_result, default="RUNNING")
        last_error: Exception | None = None
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            try:
                read_state_task = asyncio.to_thread(read_state, task_id)
                if remaining is None:
                    state = await read_state_task
                else:
                    state = await asyncio.wait_for(read_state_task, timeout=remaining)
            except asyncio.TimeoutError:
                assert poll_timeout is not None
                last_error = asyncio.TimeoutError(
                    f"Provider.read_state exceeded {poll_timeout:.1f}s"
                )
                break
            except (FileNotFoundError, KeyError, OSError) as exc:
                # The Provider may publish its registry/snapshot immediately
                # after returning from run.  A short retry handles that small
                # hand-off without blocking event consumption.
                last_error = exc
                sleep_for = _PROVIDER_HANDOFF_RETRY_SECONDS
                if remaining is not None:
                    sleep_for = min(sleep_for, remaining)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                continue
            status = _provider_status(state)
            if status not in _PROVIDER_IN_PROGRESS:
                return SimpleNamespace(
                    status=status.lower(),
                    final_node_id=getattr(state, "best_node_id", None),
                    error_code=getattr(state, "error_code", None),
                    error_message=getattr(state, "error_message", None),
                )
            last_status = status
            sleep_for = _PROVIDER_POLL_INTERVAL_SECONDS
            if deadline is not None:
                sleep_for = min(sleep_for, deadline - time.monotonic())
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        assert poll_timeout is not None
        assert deadline is not None
        elapsed = poll_timeout - max(0.0, deadline - time.monotonic())
        logger.error(
            "[RSI] Provider 未在 %.1f 秒内进入终态，task=%s status=%s error=%s",
            elapsed,
            task_id,
            last_status,
            last_error,
        )
        await self._terminate_provider_after_timeout(task_id, adapter)
        return SimpleNamespace(
            task_id=task_id,
            status="failed",
            final_node_id=getattr(initial_result, "final_node_id", None),
            error_code="PROVIDER_TIMEOUT",
            error_message="Provider 超时未进入终态",
        )

    async def _terminate_provider_after_timeout(self, task_id: str, adapter: Any) -> None:
        """Best-effort cleanup for a Provider that missed its terminal deadline.

        ``run()`` may return a live Provider task before its durable snapshot
        becomes terminal.  If polling times out, simply marking the public RSI
        task failed leaves that internal task running; a later queued task can
        then share process-global resources with it.  Ask the Provider to
        terminate, but keep a short bound so cleanup cannot wedge the worker.
        """
        terminate = getattr(adapter, "terminate", None)
        if not callable(terminate):
            logger.warning(
                "[RSI] Provider 超时但不支持 terminate，task=%s",
                task_id,
            )
            return
        try:
            result = await asyncio.wait_for(
                terminate(task_id),
                timeout=_PROVIDER_TERMINATE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - timeout cleanup is best effort
            logger.warning(
                "[RSI] Provider 超时清理失败，task=%s error=%s",
                task_id,
                exc,
            )
            return
        logger.warning(
            "[RSI] Provider 超时后已请求 terminate，task=%s status=%s",
            task_id,
            _provider_status(result, default="UNKNOWN"),
        )

    def _mark_failed_if_running(self, task_id: str, cause: str) -> None:
        try:
            if self.store.get(task_id).status == TaskStatus.RUNNING.value:
                self.store.update_status(
                    task_id,
                    [TaskStatus.RUNNING.value],
                    TaskStatus.FAILED.value,
                    cause=cause,
                )
        except RsiTaskStateConflict:
            # A concurrent pause/terminate owns the final public state.
            logger.debug("[RSI] task state changed while handling failure: %s", task_id)

    def _schedule_provider_control(self, task_id: str, adapter: Any, mode: str) -> None:
        method = getattr(adapter, mode, None)
        if not callable(method):
            raise RsiScenarioNotSupported(f"当前 Provider 不支持 {mode}")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RsiNotReady(
                f"当前无运行中事件循环，无法调用 Provider.{mode}: task={task_id}"
            )
        previous = self._control_tasks.get(task_id)
        if previous is not None and not previous.done():
            logger.warning(
                "[RSI] Provider.%s task=%s 将在前一个控制完成后执行",
                mode,
                task_id,
            )

        async def _invoke() -> Any:
            if previous is not None:
                try:
                    await previous
                except asyncio.CancelledError:
                    if not previous.cancelled():
                        raise
                except Exception as exc:  # noqa: BLE001 - continue queued control
                    logger.warning(
                        "[RSI] 前一个 Provider 控制失败，继续执行 Provider.%s task=%s: %s",
                        mode,
                        task_id,
                        exc,
                    )
            result = await method(task_id)
            self._apply_control_result(task_id, mode, result)
            return result

        control_task = loop.create_task(_invoke())
        self._control_tasks[task_id] = control_task

        def _control_done(done: asyncio.Task[Any]) -> None:
            if self._control_tasks.get(task_id) is done:
                self._control_tasks.pop(task_id, None)
            if done.cancelled():
                logger.warning("[RSI] Provider.%s control cancelled task=%s", mode, task_id)
                return
            try:
                done.result()
            except Exception as exc:  # noqa: BLE001 - keep worker alive
                logger.warning("[RSI] Provider.%s failed task=%s: %s", mode, task_id, exc)

        control_task.add_done_callback(_control_done)

    def _apply_control_result(self, task_id: str, mode: str, result: Any) -> None:
        """Commit the public state only after a Provider control returns."""
        raw_status = getattr(result, "status", result)
        raw_status = getattr(raw_status, "value", raw_status)
        status = str(raw_status or "").upper()
        target = {
            "COMPLETED": TaskStatus.COMPLETED.value,
            "FAILED": TaskStatus.FAILED.value,
            "PAUSED": TaskStatus.PAUSED.value,
            "TERMINATED": TaskStatus.TERMINATED.value,
        }.get(status)
        if target is None:
            logger.warning(
                "[RSI] Provider.%s returned an unusable status task=%s status=%r",
                mode,
                task_id,
                raw_status,
            )
            return
        try:
            current = self.store.get(task_id).status
            if current == target:
                return
            allowed_targets = {
                TaskStatus.RUNNING.value: {
                    TaskStatus.COMPLETED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.PAUSED.value,
                    TaskStatus.TERMINATED.value,
                },
                TaskStatus.PAUSED.value: {TaskStatus.TERMINATED.value},
            }
            if target not in allowed_targets.get(current, set()):
                logger.debug(
                    "[RSI] Provider.%s result ignored for task=%s current=%s target=%s",
                    mode,
                    task_id,
                    current,
                    target,
                )
                return
            self.store.update_status(
                task_id,
                [current],
                target,
                cause=f"provider.{status.lower()}",
            )
        except RsiTaskStateConflict:
            # The run path or another control already owns the final state.
            logger.debug("[RSI] task state changed while applying Provider.%s: %s", mode, task_id)

    def _persist_results(self, task_id: str, result: Any) -> None:
        """引擎结果落盘（IterativeSingleHarnessResult 形状）→ task.json.config.results。

        ``merge_results`` 由 store 在锁内完成读-改-写，避免与 update_status /
        mark_active_ref_released 并发写同一 task.json（PR !5798 #3）。
        """
        if result is None:
            return
        try:
            results: dict[str, Any] = {}
            for key in (
                "state_path",
                "report_path",
                "current_harness_refs_path",
                "best_harness_refs_path",
                "published_harness_refs_path",
                "best_score",
                "best_artifact_path",
                "published_artifact_path",
                "final_node_id",
                "error_code",
                "error_message",
            ):
                value = getattr(result, key, None)
                if value is not None:
                    results[key] = str(value) if not isinstance(value, float) else value
            # ``EngineResult`` in openjiuwen intentionally keeps a small
            # common shape; the publication paths live in the raw persisted
            # state.  Capture them when the concrete Harness adapter exposes
            # that read-only seam so the task record remains self-describing.
            if "published_harness_refs_path" not in results:
                task = self.store.get(task_id)
                adapter = self._adapter_for(task.scenario, task.artifact_type)
                reader = getattr(adapter, "read_publication_state", None)
                if callable(reader):
                    state = reader(task_id)
                    if isinstance(state, dict):
                        for key in (
                            "current_harness_refs_path",
                            "best_harness_refs_path",
                            "published_harness_refs_path",
                            "publication_status",
                        ):
                            value = state.get(key)
                            if value is not None:
                                results[key] = str(value)
            self.store.merge_results(task_id, results)
        except Exception:  # noqa: BLE001
            logger.exception("[RSI] 持久化引擎结果失败 task=%s", task_id)

    def _push(self, event_type: str):
        callback = self._push_callbacks.get(event_type)

        async def _send(task_id: str, payload: dict[str, Any]) -> None:
            if callback is None:
                return
            await callback(event_type, task_id, payload)

        return _send

    def _dequeue_locked(self, task_id: str) -> None:
        """排队中出队：从队列移除该 task（保持其余顺序）。"""
        remaining: list[str] = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item != task_id:
                remaining.append(item)
            else:
                self._queue.task_done()
        for item in remaining:
            self._queue.put_nowait(item)

    @staticmethod
    def _conflict(task_id: str, message: str) -> None:
        raise RsiTaskStateConflict(message)


def _sink(queue: asyncio.Queue[EngineEvent | None]):
    """事件入队闭包（引擎 on_event → 有界队列）。"""

    async def _put(event: EngineEvent) -> None:
        queue.put_nowait(event)

    return _put


def _provider_status(value: Any, *, default: str = "") -> str:
    raw_status = getattr(value, "status", value)
    raw_status = getattr(raw_status, "value", raw_status)
    return str(raw_status or default).upper()
