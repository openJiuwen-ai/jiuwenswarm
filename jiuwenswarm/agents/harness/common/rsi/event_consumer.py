"""RsiEventConsumer：引擎事件单协程消费者（内部 v3 §4.3）。

分发：
- ``progress.metric`` → 节流合并 → 投影 metric 快照 → P2（推送由调用方注入回调）
- ``progress.usage`` → ``RsiUsageRecorder.record`` + 实时 P2 用量推送
- ``node.created`` → 采纳时快照 + 投影 → P3
- ``node.stage`` → 投影描述更新 → P3

推送回调由 AgentServer 注入（``push_callbacks``），本层不直接依赖 WebSocket；
无回调时静默跳过推送（纯拉取模式仍可用）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.event_journal import RsiEventJournal
from jiuwenswarm.agents.harness.common.rsi.events import EngineEvent

logger = logging.getLogger(__name__)

PushCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class RsiEventConsumer:
    """单协程消费引擎事件；绑定任务上下文（task_id / push 回调）。"""

    def __init__(
        self,
        task_id: str,
        usage_recorder: Any,
        projector: Any,
        artifact_service: Any,
        event_journal: Any = None,
    ) -> None:
        self.task_id = task_id
        self.usage_recorder = usage_recorder
        self.projector = projector
        self.artifact_service = artifact_service
        tasks_root = getattr(projector, "tasks_root", None)
        self.event_journal = event_journal
        if self.event_journal is None and tasks_root is not None:
            self.event_journal = RsiEventJournal(tasks_root)
        self._on_progress: PushCallback | None = None
        self._on_tree_delta: PushCallback | None = None
        # 节流记录：P2 只推最新值（内部 v3 §4.3）
        self._last_pushed_node_ids: set[str] = set()

    def bind_push(self, *, on_progress: PushCallback | None = None, on_tree_delta: PushCallback | None = None) -> None:
        self._on_progress = on_progress
        self._on_tree_delta = on_tree_delta

    async def on_engine_event(self, event: EngineEvent | Any) -> None:
        """事件入口（同时兼容内部 dict 信封和 agent-core dataclass）。"""
        if self.event_journal is not None:
            try:
                self.event_journal.append(self.task_id, event)
            except Exception:  # noqa: BLE001 - observability must not stop training
                logger.exception("[RSI] 引擎事件日志写入失败: task=%s", self.task_id)
        provider_event_type = getattr(event, "event_type", None)
        if provider_event_type == "status":
            # TaskStore is the public status authority.  Provider status
            # events are still useful for diagnostics, but updating the store
            # here would race the worker's state machine and duplicate P1.
            return
        if provider_event_type == "progress":
            usage = getattr(event, "usage", None)
            self.projector.on_progress_metric(
                self.task_id,
                {
                    "iteration": getattr(event, "iteration", 0),
                    "total_iterations": getattr(event, "total_iterations", 0),
                    "score": getattr(event, "score", None),
                    "baseline": getattr(event, "baseline", None),
                },
            )
            self.usage_recorder.record_cumulative(
                self.task_id,
                usage,
                iteration=getattr(event, "iteration", None),
            )
            progress = self.projector.derive_progress(self.task_id)
            usage_summary = self.usage_recorder.usage_summary(self.task_id)
            if self._on_progress is not None:
                await self._on_progress(
                    self.task_id,
                    _progress_push_payload(progress, usage_summary),
                )
            return
        if provider_event_type == "node.stage":
            stage = getattr(event, "stage", None)
            stage_payload = dict(stage) if isinstance(stage, dict) else {}
            payload = {
                "node_ref": str(getattr(event, "node_ref", "") or ""),
                "stage": stage_payload,
            }
            note = getattr(event, "note", None)
            if note is not None:
                payload["note"] = str(note)
            node = self.projector.on_node_stage(self.task_id, payload)
            if node is not None and self._on_tree_delta is not None:
                await self._on_tree_delta(
                    self.task_id,
                    {"nodes": [node.to_dict()]},
                )
            return
        if provider_event_type == "node":
            node = self.projector.on_provider_node(self.task_id, getattr(event, "node", None))
            if node is not None:
                artifacts = [
                    self._to_artifact_path(item) for item in getattr(event, "artifacts", [])
                    if isinstance(item, dict) and item.get("path")
                ]
                if node.adopted and artifacts:
                    artifact_id = self.artifact_service.make_snapshot(
                        self.task_id, str(event.node.node_id), node.node_id, artifacts
                    )
                    if artifact_id:
                        node.snapshot_artifact_id = artifact_id
                        try:
                            snapshot = self.artifact_service.locate(self.task_id, artifact_id)
                        except Exception:
                            snapshot = None
                        if snapshot is not None:
                            node.extra = {
                                **(node.extra or {}),
                                "artifact_path": snapshot.path,
                            }
                        self.projector.persist_node_update(self.task_id, node)
                self._last_pushed_node_ids.add(node.node_id)
                if self._on_tree_delta is not None:
                    await self._on_tree_delta(self.task_id, {"nodes": [node.to_dict()]})
            return
        if provider_event_type == "progress.usage":
            self.usage_recorder.record_engine_event(
                self.task_id,
                {
                    "node_ref": getattr(event, "node_ref", None),
                    "model_call": getattr(event, "model_call", None),
                },
            )
            if self._on_progress is not None:
                progress = self.projector.derive_progress(self.task_id)
                usage = self.usage_recorder.usage_summary(self.task_id)
                await self._on_progress(
                    self.task_id,
                    _progress_push_payload(progress, usage),
                )
            return
        if getattr(event, "is_progress_metric", False):
            self.projector.on_progress_metric(self.task_id, event.payload)
            progress = self.projector.derive_progress(self.task_id)
            usage = self.usage_recorder.usage_summary(self.task_id)
            if self._on_progress is not None:
                await self._on_progress(
                    self.task_id,
                    _progress_push_payload(progress, usage),
                )
            return
        if getattr(event, "is_progress_usage", False):
            self.usage_recorder.record_engine_event(self.task_id, event.payload)
            if self._on_progress is not None:
                progress = self.projector.derive_progress(self.task_id)
                usage = self.usage_recorder.usage_summary(self.task_id)
                await self._on_progress(
                    self.task_id,
                    _progress_push_payload(progress, usage),
                )
            return
        if getattr(event, "is_node_created", False):
            payload = event.payload
            node_meta = payload.get("node") if isinstance(payload.get("node"), dict) else payload
            node_ref = str(node_meta.get("ref") or "") if isinstance(node_meta, dict) else ""
            adopted = bool(node_meta.get("accepted", False)) if isinstance(node_meta, dict) else False
            if adopted:
                artifacts_raw = payload.get("artifacts") or []
                artifacts = [
                    self._to_artifact_path(item)
                    for item in artifacts_raw
                    if isinstance(item, dict)
                ]
                node = self.projector.on_node_created(self.task_id, payload)
                if node is not None and artifacts:
                    artifact_id = self.artifact_service.make_snapshot(
                        self.task_id, node_ref, node.node_id, artifacts
                    )
                    if artifact_id:
                        node.snapshot_artifact_id = artifact_id
                        self.projector.persist_node_update(self.task_id, node)
            else:
                self.projector.on_node_created(self.task_id, payload)
            delta = self.projector.derive_tree_delta(self.task_id, self._last_pushed_node_ids)
            new_ids = {n.get("node_id") for n in delta.get("nodes", []) if n.get("node_id")}
            self._last_pushed_node_ids.update(new_ids)
            if self._on_tree_delta is not None and delta.get("nodes"):
                await self._on_tree_delta(self.task_id, delta)
            return
        if getattr(event, "is_node_stage", False):
            node = self.projector.on_node_stage(self.task_id, event.payload)
            if node is not None and self._on_tree_delta is not None:
                await self._on_tree_delta(
                    self.task_id,
                    {"nodes": [node.to_dict()]},
                )
            return
        logger.debug(
            "[RSI] 未识别事件: %s/%s",
            getattr(event, "family", ""),
            getattr(event, "kind", ""),
        )

    @staticmethod
    def _to_artifact_path(item: dict[str, Any]) -> Any:
        from jiuwenswarm.agents.harness.common.rsi.models import RsiArtifactPath

        return RsiArtifactPath(
            role=str(item.get("role") or ""),
            path=str(item.get("path") or ""),
            format=str(item.get("format") or ""),
        )


async def consume_queue(
    queue: asyncio.Queue[EngineEvent | None],
    consumer: RsiEventConsumer,
) -> None:
    """单协程消费有界队列（内部 v3 §4.2 事件链路）。

    ``None`` 哨兵 = 结束信号（终态后 ``await q.join()`` 排空再关）。
    """
    while True:
        event = await queue.get()
        try:
            if event is None:
                return
            await consumer.on_engine_event(event)
        except Exception:  # noqa: BLE001 - 单事件失败不终止消费者
            logger.exception(
                "[RSI] 事件消费失败: %s",
                getattr(event, "event_type", f"{getattr(event, 'family', '')}/{getattr(event, 'kind', '')}"),
            )
        finally:
            queue.task_done()


def _progress_push_payload(
    progress: dict[str, Any],
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the v0.3 nested payload and retain legacy flat fields."""

    payload: dict[str, Any] = {
        "progress": dict(progress),
        # Older Harness consumers read iteration/score directly.  Keeping
        # these aliases is harmless while the nested v0.3 shape is adopted.
        **progress,
    }
    if usage is not None:
        payload["usage"] = usage
    return payload
