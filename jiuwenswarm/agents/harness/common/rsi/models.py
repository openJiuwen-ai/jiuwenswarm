"""RSI 服务域公共模型（与 web 契约 v0.3 / 内部接口 v3 对齐）。

- 枚举严格采用 web §3.3 全大写：status / scenario / artifact_type / node type。
- usage/token 统一结构（§3.4）。
- ``RsiTask`` 为 adapter 只读视图 + ``RsiTaskStore`` 持久化对象二合一：
  内部 v3 §4.1 出参（task_id/hame/scenario/artifact_type/status/config/run_dir/created_at）。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

TASK_ID_PREFIX = "rsi-"


def generate_task_id() -> str:
    """生成 ``rsi-<uuid8>`` 任务 ID（内部 v3 §4.1）。"""
    return f"{TASK_ID_PREFIX}{uuid.uuid4().hex[:8]}"


class TaskStatus(str, Enum):
    """任务状态全集（web §3.3）。"""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"

    @classmethod
    def terminal(cls) -> frozenset["TaskStatus"]:
        return frozenset({cls.COMPLETED, cls.FAILED, cls.TERMINATED})


class Scenario(str, Enum):
    HARNESS = "HARNESS"
    ARTIFACT = "ARTIFACT"


class ArtifactType(str, Enum):
    PAPER = "PAPER"
    PROGRAM = "PROGRAM"


class ArtifactKind(str, Enum):
    HARNESS_PLUGIN = "harness_plugin"
    ARTIFACT_PACKAGE = "artifact_package"


@dataclass(slots=True)
class Tokens:
    """§3.4 token 明细。"""

    input: int = 0
    output: int = 0
    cache_hit: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"input": self.input, "output": self.output, "cache_hit": self.cache_hit}


@dataclass(slots=True)
class Usage:
    """§3.4 usage 统一结构。"""

    tokens: Tokens = field(default_factory=Tokens)
    cost_estimate: float = 0.0
    call_count: int = 0

    def merge(self, other: "Usage") -> None:
        """聚合另一份用量（服务侧聚合，批合并属后续优化项）。"""
        self.tokens.input += other.tokens.input
        self.tokens.output += other.tokens.output
        self.tokens.cache_hit += other.tokens.cache_hit
        self.cost_estimate += other.cost_estimate
        self.call_count += other.call_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens.to_dict(),
            "cost_estimate": self.cost_estimate,
            "call_count": self.call_count,
        }


@dataclass(slots=True)
class RsiModelCall:
    """progress.usage 事件载荷（内部 v3 §3.3）。"""

    model: str
    call_count: int = 1
    tokens: Tokens = field(default_factory=Tokens)


@dataclass(slots=True)
class RsiTask:
    """任务存储对象（内部 v3 §4.1 出参 + 持久化字段）。"""

    task_id: str
    name: str
    scenario: str
    status: str
    created_at: str
    artifact_type: str | None = None
    input_file: str | None = None
    model_refs: dict[str, Any] = field(default_factory=dict)
    max_iterations: int = 1
    search_width: int = 1
    optimization_instruction: str | None = None
    artifact_path: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""
    updated_at: str | None = None
    status_history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # ``search_width`` is deprecated and must not leak into new task.json
        # snapshots.  Older persisted task files remain readable via
        # ``from_dict``, but new serialization omits the field.
        payload.pop("search_width", None)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RsiTask":
        known = {
            k
            for k in (
                "task_id", "name", "scenario", "status", "created_at", "artifact_type",
                "input_file", "model_refs", "max_iterations", "search_width",
                "optimization_instruction", "artifact_path", "config", "run_dir", "updated_at",
                "status_history",
            )
        }
        return cls(**{k: v for k, v in data.items() if k in known})

    # ---- web 投影 ----

    def list_projection(self) -> dict[str, Any]:
        """``rsi.task.list`` 六字段投影（web §6.2）。"""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "scenario": self.scenario,
            "artifact_type": self.artifact_type,
            "status": self.status,
            "created_at": self.created_at,
        }

    def get_projection(
        self,
        progress: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        best_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``rsi.task.get`` 出参（web §6.3）。"""
        config: dict[str, Any] = {
            "model": {
                "optimizer": (self.model_refs or {}).get("optimizer"),
                "tester": (self.model_refs or {}).get("tester"),
            },
            "input_file": self.input_file,
            "max_iterations": self.max_iterations,
            "optimization_instruction": self.optimization_instruction,
            "artifact_path": self.artifact_path,
            # The URL may contain proxy credentials.  Return only whether a
            # task-scoped proxy was configured.
            "web_proxy_configured": bool((self.config or {}).get("web_proxy")),
        }
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "name": self.name,
            "scenario": self.scenario,
            "artifact_type": self.artifact_type,
            "status": self.status,
            "config": config,
            "progress": progress,
            "best_artifact": best_artifact,
        }
        if usage is not None:
            payload["usage"] = usage
        if self.status == TaskStatus.FAILED.value:
            for item in reversed(self.status_history):
                if item.get("to") != TaskStatus.FAILED.value:
                    continue
                reason = str(item.get("cause") or "").strip()
                if reason:
                    payload["failure_reason"] = reason
                break
        return payload

    def to_taskview(self) -> "RsiTaskView":
        return RsiTaskView(
            task_id=self.task_id,
            scenario=self.scenario,
            artifact_type=self.artifact_type,
            input_file=self.input_file,
            model_refs=self.model_refs,
            max_iterations=self.max_iterations,
            search_width=self.search_width,
            config=self.config,
            run_dir=self.run_dir,
            optimization_instruction=self.optimization_instruction,
            artifact_path=self.artifact_path,
        )


@dataclass(slots=True)
class RsiTaskView:
    """adapter 只读任务视图（内部 v3 §5.1 ``RsiTask`` Protocol 字段）。"""

    task_id: str
    scenario: str
    artifact_type: str | None
    input_file: str | None
    model_refs: dict[str, Any]
    max_iterations: int
    search_width: int
    config: dict[str, Any]
    run_dir: str
    # Keep the original public values available to adapters.  Defaults retain
    # compatibility with older positional construction sites.
    optimization_instruction: str | None = None
    artifact_path: str | None = None


@dataclass(frozen=True, slots=True)
class RsiDatasetResult:
    """``rsi.dataset.validate`` 出参（adapter validate_input）。"""

    valid: bool
    sample_count: int | None
    errors: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "sample_count": self.sample_count, "errors": self.errors}


@dataclass(frozen=True, slots=True)
class RsiArtifactPath:
    """``node.created.artifacts[]`` 元素（内部 v3 §4.5）。"""

    role: str
    path: str
    format: str = ""


@dataclass(slots=True)
class RsiTreeNode:
    """演进树节点（web §9.1 字段）。"""

    node_id: str
    iteration: int
    parent_id: str | None
    type: str
    adopted: bool
    score: float | None
    description: str | None
    snapshot_artifact_id: str | None = None
    failure_reason: str | None = None
    failure_class: str | None = None
    changes: list[dict[str, str]] | None = None
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RsiTreeNode":
        known = {
            "node_id", "iteration", "parent_id", "type", "adopted", "score",
            "description", "snapshot_artifact_id", "failure_reason", "failure_class",
            "changes", "extra",
        }
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class RsiArtifactFile:
    """``RsiArtifactService.locate`` 出参（内部 v3 §4.5）。"""

    path: str
    kind: str
    is_best: bool


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
