"""Artifact Provider adapter and public projection helpers.

The artifact optimization implementations live outside JiuwenSwarm.  This
module is the AgentServer-owned seam between the common RSI service domain and
the Provider contracts exposed by ``openjiuwen``.  It deliberately supports
both the currently published ``ArtifactEngineRequest.model_config`` field and
the newer ``model`` field described by the integration design, so the service
does not need another change when the Provider implementation lands.
"""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiNotReady,
    RsiPathInvalid,
    RsiScenarioNotSupported,
)
from jiuwenswarm.agents.harness.common.rsi.models import (
    RsiDatasetResult,
    RsiTaskView,
)


def _plain(value: Any) -> Any:
    """Convert Provider dataclasses and nested values to JSON-shaped data."""

    # The paper tree implementation in agent-core uses Pydantic models for
    # its durable snapshots, while the older artifact providers use
    # dataclasses.  Keep this boundary structural so JiuwenSwarm does not
    # import either provider's private model classes.
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _plain(model_dump(mode="python"))
        except TypeError:
            return _plain(model_dump())
    model_dict = getattr(value, "dict", None)
    if callable(model_dict):
        return _plain(model_dict())
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def provider_status(value: Any, *, default: str = "CREATED") -> str:
    """Normalize agent-core's lower-case status enum to the Web contract."""

    raw = getattr(value, "value", value)
    return str(raw or default).upper()


def provider_usage_to_dict(usage: Any) -> dict[str, Any] | None:
    """Project an agent-core ``RsiUsage`` value to the common JSON shape."""

    if usage is None:
        return None
    raw = _plain(usage)
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    return {
        "tokens": {
            "input": _safe_int(tokens.get("input")),
            "output": _safe_int(tokens.get("output")),
            "cache_hit": _safe_int(tokens.get("cache_hit")),
        },
        "cost_estimate": _safe_float(raw.get("cost_estimate"), default=0.0),
        "call_count": _safe_int(raw.get("call_count")),
    }


def provider_artifact_to_dict(artifact: Any) -> dict[str, Any]:
    """Project one Provider artifact reference without inventing a URL."""

    raw = _plain(artifact)
    return raw if isinstance(raw, dict) else {}


def provider_node_to_dict(node: Any) -> dict[str, Any]:
    """Project a Provider node to the Web-facing common node shape."""

    raw = _plain(node)
    if not isinstance(raw, dict):
        return {}
    node_type = str(raw.get("type") or "").upper()
    paper = (raw.get("extra") or {}).get("paper") or {}
    program = (raw.get("extra") or {}).get("program")
    if node_type in {"CANDIDATE", "RUNNING"}:
        normalized_type = "PROVISIONAL"
    elif program is not None and node_type == "ADOPTED" and not raw.get("adopted"):
        normalized_type = "REJECTED"
    elif node_type == "REPORTING" and paper.get("outcome") == "pending":
        normalized_type = "PROVISIONAL"
    elif node_type in {"ROOT", "ADOPTED", "REJECTED", "PROVISIONAL", "PRUNED"}:
        normalized_type = node_type
    elif node_type in {"CANDIDATE", "REPORTING", "SUCCESS"}:
        normalized_type = "ADOPTED" if bool(raw.get("adopted")) else "REJECTED"
    else:
        normalized_type = "ADOPTED" if bool(raw.get("adopted")) else "REJECTED"

    changes: list[dict[str, Any]] = []
    for change in raw.get("changes") or []:
        if isinstance(change, dict):
            # Keep the legacy ``element`` field while preserving the richer
            # Provider intent record for node-detail rendering.
            group = str(
                change.get("group")
                or change.get("element")
                or change.get("domain")
                or ""
            ).upper()
            summary = change.get("summary") or change.get("reason") or change.get("description")
            changes.append(
                {
                    "group": group,
                    "element": group,
                    "operation": str(change.get("operation") or "").upper(),
                    "function": change.get("function") or change.get("function_name"),
                    "target": change.get("target") or change.get("member") or change.get("path"),
                    "summary": summary,
                    "reason": summary,
                }
            )

    extra = raw.get("extra")
    summary = raw.get("summary") if raw.get("summary") is not None else raw.get("description")
    reason = raw.get("reason") if raw.get("reason") is not None else raw.get("failure_reason")
    return {
        "node_id": str(raw.get("node_id") or ""),
        "iteration": _safe_int(raw.get("iteration")),
        "parent_id": raw.get("parent_id"),
        "type": normalized_type,
        "adopted": bool(raw.get("adopted")),
        "score": _safe_float_or_none(raw.get("score")),
        "summary": summary,
        "description": summary,
        "snapshot_artifact_id": raw.get("snapshot_artifact_id"),
        "reason": reason,
        "failure_reason": reason,
        "failure_class": raw.get("failure_class"),
        "changes": changes,
        "extra": dict(extra) if isinstance(extra, dict) else {},
    }


def provider_tree_to_web(tree: Any) -> dict[str, Any]:
    """Project ``TreeResponse`` while preserving one node shape for push/pull."""

    raw = _plain(tree)
    if not isinstance(raw, dict):
        return {"nodes": [], "depth": 0, "iteration": 0}
    nodes = [provider_node_to_dict(node) for node in raw.get("nodes") or []]
    return {
        "nodes": nodes,
        "depth": _safe_int(raw.get("depth")),
        "iteration": _safe_int(raw.get("iteration")),
    }


def provider_best_artifact(report: Any) -> dict[str, Any] | None:
    """Build the compact ``best_artifact`` object used by task/report APIs."""

    raw = _plain(report)
    if not isinstance(raw, dict):
        return None
    best_node_id = raw.get("best_node_id")
    refs = raw.get("artifact_index") or []
    best: dict[str, Any] | None = None
    # The report can contain a module-level node package followed by the
    # complete iteration snapshot.  Both may point at the same best node;
    # the newest ref is the one the task-level download should expose.
    for ref in reversed(refs):
        if not isinstance(ref, dict):
            continue
        if best_node_id is not None and ref.get("node_id") == best_node_id:
            best = ref
            break
    if best is None and refs:
        candidate = refs[-1]
        if isinstance(candidate, dict):
            best = candidate
    if best is None:
        return None
    return {
        "artifact_id": best.get("artifact_id"),
        "name": best.get("name"),
        "adopted": True,
    }


def provider_report_to_web(report: Any, state: Any = None) -> dict[str, Any]:
    """Project an artifact Provider report to ``rsi.report.get``."""

    raw = _plain(report)
    if not isinstance(raw, dict):
        raw = {}
    state_raw = _plain(state)
    if not isinstance(state_raw, dict):
        state_raw = {}
    usage = provider_usage_to_dict(raw.get("usage"))
    if usage is None:
        usage = provider_usage_to_dict(state_raw.get("usage"))
    raw_metrics = raw.get("metrics")
    metrics = dict(raw_metrics) if isinstance(raw_metrics, dict) else {}
    metrics.setdefault("eval_passed", 0)
    metrics.setdefault("eval_total", 0)
    metrics.setdefault("pruned_count", 0)
    metrics.setdefault("iterations", _safe_int(state_raw.get("iteration")))
    best_artifact = provider_best_artifact(report)
    if not metrics.get("best_artifact_id"):
        metrics["best_artifact_id"] = best_artifact.get("artifact_id") if best_artifact else None
    return {
        "status": provider_status(raw.get("status") or state_raw.get("status")),
        "best_score": _safe_float_or_none(
            raw.get("best_score") if raw.get("best_score") is not None else state_raw.get("score")
        ),
        "baseline": _safe_float_or_none(
            raw.get("baseline") if raw.get("baseline") is not None else state_raw.get("baseline")
        ),
        "metrics": metrics,
        "usage": usage,
        "best_artifact": best_artifact,
        "report_summary": str(raw.get("summary") or ""),
        "markdown": None,
    }


def provider_state_to_progress(state: Any) -> dict[str, Any]:
    """Project a Provider state snapshot to the common progress shape."""

    raw = _plain(state)
    if not isinstance(raw, dict):
        return {
            "iteration": 0,
            "total_iterations": 0,
            "score": None,
            "baseline": None,
        }
    return {
        "iteration": _safe_int(raw.get("iteration")),
        "total_iterations": _safe_int(raw.get("total_iterations")),
        "score": _safe_float_or_none(raw.get("score")),
        "baseline": _safe_float_or_none(raw.get("baseline")),
    }


class ArtifactEngineAdapter:
    """Adapt one program/paper Provider to the common RSI worker contract."""

    def __init__(
        self,
        artifact_type: str,
        provider: Any,
        *,
        model_resolver: Callable[[str | None], Any] | None = None,
        requires_model: bool = True,
    ) -> None:
        normalized = str(artifact_type or "").strip().upper()
        if normalized not in {"PROGRAM", "PAPER"}:
            raise RsiScenarioNotSupported(f"不支持的 artifact_type: {artifact_type}")
        self.artifact_type = normalized
        self.provider = provider
        self._model_resolver = model_resolver
        self._requires_model = requires_model
        self.supports_pause = bool(getattr(provider, "supports_pause", normalized == "PROGRAM"))
        self.supports_resume = bool(getattr(provider, "supports_resume", normalized == "PROGRAM"))

    def build_request(self, task: RsiTaskView, *, resume: bool = False) -> Any:
        """Build the current agent-core request without Provider-side policy."""

        if task.scenario.upper() != "ARTIFACT":
            raise RsiScenarioNotSupported("ArtifactEngineAdapter 只能处理 ARTIFACT 任务")
        if str(task.artifact_type or "").upper() != self.artifact_type:
            raise RsiScenarioNotSupported(
                f"任务 artifact_type={task.artifact_type!r} 与 adapter={self.artifact_type!r} 不匹配"
            )
        model_id = str((task.model_refs or {}).get("optimizer") or "").strip()
        if not model_id:
            raise RsiScenarioNotSupported("artifact 任务缺少 optimizer model")

        from openjiuwen.rsi.artifact_rsi.request import ArtifactEngineRequest

        request_fields = {item.name for item in fields(ArtifactEngineRequest)}
        kwargs: dict[str, Any] = {
            "task_id": task.task_id,
            "run_dir": task.run_dir,
            "artifact_path": task.artifact_path or task.config.get("artifact_path"),
            "max_iterations": task.max_iterations,
            "optimization_instruction": (
                (
                    task.optimization_instruction
                    or task.config.get("optimization_instruction")
                    or task.config.get("instruction")
                )
                if self.artifact_type == "PAPER"
                else None
            ),
        }
        web_proxy = str(task.config.get("web_proxy") or "").strip() or None
        if web_proxy and self.artifact_type == "PAPER":
            kwargs["web_proxy"] = web_proxy
        if "resume" in request_fields:
            kwargs["resume"] = resume

        # agent-core currently exposes ``model_config`` as the Provider-facing
        # placeholder.  The reviewed integration contract changes this field to
        # a resolved ``model`` object.  Support both shapes at the boundary.
        resolved_model = self._model_resolver(model_id) if self._model_resolver else None
        if "model" in request_fields:
            if resolved_model is None and self._requires_model:
                raise RsiNotReady(f"optimizer model 未注册: {model_id}")
            kwargs["model"] = resolved_model
        elif "model_config" in request_fields:
            kwargs["model_config"] = model_id
        else:
            raise RsiNotReady("agent-core ArtifactEngineRequest 缺少 model/model_config 字段")

        return ArtifactEngineRequest(**{key: value for key, value in kwargs.items() if key in request_fields})

    def validate_input(
        self,
        path: str | None,
        *,
        scenario: str = "ARTIFACT",
        artifact_type: str | None = None,
    ) -> RsiDatasetResult:
        """Delegate path semantics to the selected Provider."""

        if str(scenario).upper() != "ARTIFACT":
            raise RsiScenarioNotSupported("ArtifactEngineAdapter 只能处理 ARTIFACT 场景")
        if artifact_type and str(artifact_type).upper() != self.artifact_type:
            raise RsiScenarioNotSupported("artifact_type 与 Provider 不匹配")
        result = self.provider.validate_input(path)
        raw = _plain(result)
        if not isinstance(raw, dict):
            raise RsiNotReady("artifact Provider 返回了无法识别的校验结果")
        errors = []
        for error in raw.get("errors") or []:
            if not isinstance(error, dict):
                continue
            errors.append(
                {
                    "reason": str(error.get("message") or error.get("reason") or "输入校验失败"),
                    "code": str(error.get("code") or "DATASET_INVALID"),
                }
            )
        sample_count = 1 if bool(raw.get("valid")) and path else None
        return RsiDatasetResult(valid=bool(raw.get("valid")), sample_count=sample_count, errors=errors)

    async def run(self, request: Any, *, on_event: Any = None) -> Any:
        return await self.provider.run(request, on_event=on_event)

    async def resume(self, request: Any, *, on_event: Any = None) -> Any:
        return await self.provider.resume(request, on_event=on_event)

    async def pause(self, task_id: str, *, on_event: Any = None) -> Any:
        return await self.provider.pause(task_id, on_event=on_event)

    async def terminate(self, task_id: str, *, on_event: Any = None) -> Any:
        return await self.provider.terminate(task_id, on_event=on_event)

    def read_state(self, task_id: str) -> Any:
        return self.provider.read_state(task_id)

    def read_report(self, task_id: str) -> Any:
        return self.provider.read_report(task_id)

    def get_tree(self, task_id: str) -> Any:
        return self.provider.get_tree(task_id)

    def locate_artifact(self, task_id: str, artifact_id: str | None = None) -> Any:
        return self.provider.locate_artifact(task_id, artifact_id)


def validate_provider_artifact_path(path: str | None, *, allow_missing: bool = False) -> Path | None:
    """Validate a Provider artifact path for the AgentServer download seam."""

    if not path:
        if allow_missing:
            return None
        raise RsiPathInvalid("artifact 路径不能为空")
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise RsiPathInvalid(f"产物路径不存在: {resolved}")
    return resolved


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any, *, default: float) -> float:
    parsed = _safe_float_or_none(value)
    return default if parsed is None else parsed


def _safe_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ArtifactEngineAdapter",
    "provider_artifact_to_dict",
    "provider_best_artifact",
    "provider_node_to_dict",
    "provider_report_to_web",
    "provider_state_to_progress",
    "provider_status",
    "provider_tree_to_web",
    "provider_usage_to_dict",
    "validate_provider_artifact_path",
]
