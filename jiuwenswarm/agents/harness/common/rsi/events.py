"""引擎事件模型（内部 v3 §3.3 四子类 + 公共信封）。

- 信封：``event_id``（任务内调试序号，不做跨仓协议）/ ``task_id`` / ``ts`` / ``family`` / ``kind``。
- 四子类：``progress.metric`` / ``progress.usage`` / ``node.created`` / ``node.stage``。
- 实现按事件基类 + 载荷 dict 泛化：引擎事件为草案基线（不锁版本），字段级解析收口在本模块；
  ``RsiEventConsumer``/``RsiProjector`` 只消费规范化字段，便于 agent-core 事件化落地后微调。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EngineEvent:
    """公共信封 + 规范化载荷。"""

    family: str
    kind: str
    task_id: str = ""
    event_id: int | None = None
    ts: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_progress_metric(self) -> bool:
        return self.family == "progress" and self.kind == "metric"

    @property
    def is_progress_usage(self) -> bool:
        return self.family == "progress" and self.kind == "usage"

    @property
    def is_node_created(self) -> bool:
        return self.family == "node" and self.kind == "created"

    @property
    def is_node_stage(self) -> bool:
        return self.family == "node" and self.kind == "stage"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "ts": self.ts,
            "family": self.family,
            "kind": self.kind,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EngineEvent":
        return cls(
            family=str(data.get("family") or ""),
            kind=str(data.get("kind") or ""),
            task_id=str(data.get("task_id") or ""),
            event_id=data.get("event_id"),
            ts=str(data.get("ts") or ""),
            payload=dict(data.get("payload") or {}),
        )


# ---------------------------------------------------------------------------
# 事件装配辅助：engine 事件 dict（agent-core 侧形状）→ 内部规范化事件
# ---------------------------------------------------------------------------

_EVENT_KIND_BY_FAMILY: dict[str, dict[str, str]] = {
    "progress": {"metric": "metric", "usage": "usage"},
    "node": {"created": "created", "stage": "stage"},
}


def parse_engine_event(raw: dict[str, Any], *, default_task_id: str = "") -> EngineEvent | None:
    """把引擎侧事件 dict 规范化为 ``EngineEvent``。

    兼容两种形态：
    1. 引擎未来事件化（agent-core 发出，Zhiting）：信封字段在顶层（family/kind/task_id/ts/event_id），载荷内联或 payload；
    2. 内部事件（本项目队列内）已规范化：直接透传 ``EngineEvent.to_dict()``。
    """
    if raw.get("family") and raw.get("kind"):
        family = str(raw["family"])
        kind = str(raw["kind"])
        kind_map = _EVENT_KIND_BY_FAMILY.get(family)
        if not kind_map or kind not in kind_map.values():
            return None
        payload = dict(raw.get("payload") or {})
        if not payload:
            # 顶层扁平载荷 → 收口到 payload（agent-core 事件化常见形态）
            for key in ("iteration", "score", "baseline", "node_ref", "model_call", "node", "artifacts", "stage"):
                if key in raw:
                    payload[key] = raw[key]
        return EngineEvent(
            family=family,
            kind=kind,
            task_id=str(raw.get("task_id") or default_task_id),
            event_id=int(raw["event_id"]) if raw.get("event_id") is not None else None,
            ts=str(raw.get("ts") or ""),
            payload=payload,
        )
    if raw.get("family") is None and raw.get("kind") is None and "payload" in raw:
        return EngineEvent.from_dict(raw)
    return None