"""RsiUsageRecorder：progress.usage 事件聚合（内部 v3 §4.6）。

- ``record(task_id, node_ref, model_call)``：每次模型调用一条；服务侧按节点/任务聚合。
- ``get(task_id)``：出参 = web §3.4 usage 统一结构 + per_iteration/usage_by_node（§8.2）。
- 费用单价算法（C1）为**中优先级预留**：``cost_estimate`` 默认 0.0 占位，不实现单价。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.errors import RsiTaskNotFound
from jiuwenswarm.agents.harness.common.rsi.models import RsiModelCall, Tokens, Usage

_ROOT_NODE = "ROOT"


class RsiUsageRecorder:
    """用量归属与聚合（内存态；任务级持久化由 TaskStore for 后续版本）。

    约束：接口返回统一 usage 结构（§3.4）；单价算法遗留（§13 TODO 2），
    仅做归属与聚合，保证字段形状可用。
    """

    def __init__(self) -> None:
        self._by_node: dict[str, dict[str, Usage]] = {}  # task_id -> {node_id: Usage}
        self._node_sequence: dict[str, list[str]] = {}  # task_id -> 派生节点序
        # Artifact Providers report cumulative usage snapshots rather than
        # one model-call event.  Keep this stream separate so repeated P2
        # events do not get added together.
        self._cumulative: dict[str, Usage] = {}
        self._cumulative_by_iteration: dict[str, dict[int, Usage]] = {}

    def record(
        self,
        task_id: str,
        node_ref: str | None,
        model_call: RsiModelCall,
    ) -> None:
        node_id = node_ref or _ROOT_NODE
        if task_id not in self._by_node:
            self._by_node[task_id] = {}
        usage = self._by_node[task_id].setdefault(node_id, Usage())
        if node_id != _ROOT_NODE and node_id not in self._node_sequence.setdefault(task_id, []):
            self._node_sequence[task_id].append(node_id)
        usage.merge(
            Usage(
                tokens=model_call.tokens,
                call_count=model_call.call_count,
            )
        )

    def record_engine_event(self, task_id: str, payload: dict[str, Any]) -> None:
        """从 ``progress.usage`` 事件载荷归一记录（内部 v3 §3.3）。"""
        model_call_raw = payload.get("model_call")
        if is_dataclass(model_call_raw):
            model_call_raw = asdict(model_call_raw)
        if not isinstance(model_call_raw, dict):
            return
        tokens_raw = model_call_raw.get("tokens") or {}
        if is_dataclass(tokens_raw):
            tokens_raw = asdict(tokens_raw)
        tokens = Tokens(
            input=int(tokens_raw.get("input") or 0),
            output=int(tokens_raw.get("output") or 0),
            cache_hit=int(tokens_raw.get("cache_hit") or 0),
        )
        model_call = RsiModelCall(
            model=str(model_call_raw.get("model") or ""),
            call_count=int(model_call_raw.get("call_count") or 1),
            tokens=tokens,
        )
        self.record(task_id, payload.get("node_ref"), model_call)

    def record_cumulative(self, task_id: str, usage: Any, *, iteration: int | None = None) -> None:
        """Record a Provider cumulative usage snapshot idempotently."""
        parsed = _usage_from_value(usage)
        if parsed is None:
            return
        self._cumulative[task_id] = parsed
        if iteration is not None:
            try:
                index = int(iteration)
            except (TypeError, ValueError):
                index = 0
            if index > 0:
                self._cumulative_by_iteration.setdefault(task_id, {})[index] = parsed

    def get(self, task_id: str) -> dict[str, Any]:
        """``rsi.usage.get`` 出参（web §8.2）。"""
        if task_id not in self._by_node and task_id not in self._cumulative:
            raise RsiTaskNotFound(task_id)
        nodes = self._by_node.get(task_id, {})
        total = _copy_usage(self._cumulative.get(task_id)) if task_id in self._cumulative else Usage()
        if task_id not in self._cumulative:
            for usage in nodes.values():
                total.merge(usage)
        if task_id in self._cumulative:
            per_iteration = [
                {"iteration": index, "usage": usage.to_dict()}
                for index, usage in sorted(self._cumulative_by_iteration.get(task_id, {}).items())
            ]
        else:
            per_iteration = [
                {"iteration": index, "usage": nodes.get(node_id, Usage()).to_dict()}
                for index, node_id in enumerate(self._node_sequence.get(task_id, []), start=1)
            ]
        usage_by_node = {node_id: usage.to_dict() for node_id, usage in nodes.items()}
        return {
            "usage": total.to_dict(),
            "per_iteration": per_iteration,
            "usage_by_node": usage_by_node,
        }

    def usage_summary(self, task_id: str) -> dict[str, Any] | None:
        """``rsi.task.get`` 的 usage 摘要（仅当有记录）。"""
        try:
            return self.get(task_id)["usage"]
        except RsiTaskNotFound:
            return None


def _usage_from_value(value: Any) -> Usage | None:
    """Convert Provider dataclasses or dicts without coupling to agent-core."""
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            raw = model_dump(mode="python")
        except TypeError:
            raw = model_dump()
    else:
        model_dict = getattr(value, "dict", None)
        if callable(model_dict):
            raw = model_dict()
        else:
            raw = asdict(value) if is_dataclass(value) else value
    if not isinstance(raw, Mapping):
        return None
    tokens_raw = raw.get("tokens")
    token_model_dump = getattr(tokens_raw, "model_dump", None)
    if callable(token_model_dump):
        try:
            tokens_raw = token_model_dump(mode="python")
        except TypeError:
            tokens_raw = token_model_dump()
    else:
        token_model_dict = getattr(tokens_raw, "dict", None)
        if callable(token_model_dict):
            tokens_raw = token_model_dict()
    if is_dataclass(tokens_raw):
        tokens_raw = asdict(tokens_raw)
    if not isinstance(tokens_raw, Mapping):
        tokens_raw = {}
    return Usage(
        tokens=Tokens(
            input=_safe_int(tokens_raw.get("input")),
            output=_safe_int(tokens_raw.get("output")),
            cache_hit=_safe_int(tokens_raw.get("cache_hit")),
        ),
        cost_estimate=_safe_float(raw.get("cost_estimate")),
        call_count=_safe_int(raw.get("call_count")),
    )


def _copy_usage(value: Usage | None) -> Usage:
    if value is None:
        return Usage()
    return Usage(
        tokens=Tokens(
            input=value.tokens.input,
            output=value.tokens.output,
            cache_hit=value.tokens.cache_hit,
        ),
        cost_estimate=value.cost_estimate,
        call_count=value.call_count,
    )


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
