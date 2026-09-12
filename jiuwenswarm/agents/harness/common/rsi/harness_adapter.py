"""Harness Provider adapter owned by the JiuwenSwarm RSI service.

The artifact RSI integration already has a Provider boundary in
``artifact_adapter.py``.  Harness RSI exposes an orchestrator from agent-core
rather than the service's resumable Provider contract, so this module defines
the small, stable request shape that the service uses for both the mock
Provider and the production ``HarnessProvider`` implementation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from collections.abc import Mapping
from typing import Any, Protocol

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiNotReady,
    RsiScenarioNotSupported,
)
from jiuwenswarm.agents.harness.common.rsi.models import (
    RsiDatasetResult,
    RsiTaskView,
)


@dataclass(frozen=True, slots=True)
class HarnessEngineRequest:
    """Stable request passed from the RSI worker to a Harness Provider.

    ``agent-core``'s ``IterativeSingleHarnessRequest`` intentionally stays
    focused on the orchestrator inputs.  RSI also needs task identity,
    search parameters, and model metadata at this boundary, so those values
    are kept here and translated by the concrete Provider when necessary.
    """

    task_id: str
    dataset_files: tuple[str, ...]
    harness_refs_path: str
    output_dir: str
    dataset_id: str
    max_iterations: int
    search_width: int
    model_refs: dict[str, Any]
    optimization_instruction: str | None = None
    resume: bool = False
    orchestrator_config_path: str | None = None


class HarnessProviderContract(Protocol):
    """Protocol implemented by ``HarnessProvider`` and its mock."""

    supports_pause: bool
    supports_resume: bool
    supports_terminate: bool

    def validate_input(self, path: str | None) -> Any: ...

    async def run(self, request: HarnessEngineRequest, *, on_event: Any = None) -> Any: ...

    async def resume(self, request: HarnessEngineRequest, *, on_event: Any = None) -> Any: ...

    async def pause(self, task_id: str) -> Any: ...

    async def terminate(self, task_id: str) -> Any: ...

    def read_state(self, task_id: str) -> Any: ...

    def read_publication_state(self, task_id: str) -> dict[str, Any]: ...

    def read_report(self, task_id: str) -> Any: ...

    def get_tree(self, task_id: str) -> Any: ...

    def locate_artifact(self, task_id: str, artifact_id: str | None = None) -> Any: ...


class HarnessEngineAdapter:
    """Adapt one ``HarnessProvider`` to the common RSI worker contract."""

    def __init__(self, provider: HarnessProviderContract) -> None:
        self.provider = provider
        self.supports_pause = bool(getattr(provider, "supports_pause", True))
        self.supports_resume = bool(getattr(provider, "supports_resume", True))
        self.supports_terminate = bool(getattr(provider, "supports_terminate", True))

    def build_request(self, task: RsiTaskView, *, resume: bool = False) -> HarnessEngineRequest:
        if str(task.scenario or "").upper() != "HARNESS":
            raise RsiScenarioNotSupported("HarnessEngineAdapter 只能处理 HARNESS 任务")
        if not task.input_file:
            raise RsiScenarioNotSupported("harness 任务缺少 input_file")

        config = task.config if isinstance(task.config, dict) else {}
        refs_path = str(config.get("harness_refs_path") or "").strip()
        orchestrator_config_path = str(
            config.get("orchestrator_config_path") or ""
        ).strip() or None
        dataset_id = str(config.get("dataset_id") or f"{task.task_id}:dataset")
        return HarnessEngineRequest(
            task_id=task.task_id,
            dataset_files=(str(task.input_file),),
            harness_refs_path=refs_path,
            output_dir=task.run_dir,
            dataset_id=dataset_id,
            max_iterations=max(1, int(task.max_iterations or 1)),
            search_width=max(1, int(task.search_width or 1)),
            model_refs=dict(task.model_refs or {}),
            optimization_instruction=task.optimization_instruction,
            resume=resume,
            orchestrator_config_path=orchestrator_config_path,
        )

    def validate_input(
        self,
        path: str | None,
        *,
        scenario: str = "HARNESS",
        artifact_type: str | None = None,
    ) -> RsiDatasetResult:
        if str(scenario or "").upper() != "HARNESS":
            raise RsiScenarioNotSupported("HarnessEngineAdapter 只能处理 HARNESS 场景")
        if artifact_type:
            raise RsiScenarioNotSupported("harness 场景不接受 artifact_type")
        provider_result = self.provider.validate_input(path)
        return _dataset_result(provider_result)

    async def run(self, request: HarnessEngineRequest, *, on_event: Any = None) -> Any:
        return await self.provider.run(request, on_event=on_event)

    async def resume(self, request: HarnessEngineRequest, *, on_event: Any = None) -> Any:
        return await self.provider.resume(request, on_event=on_event)

    async def pause(self, task_id: str, *, on_event: Any = None) -> Any:
        del on_event
        return await self.provider.pause(task_id)

    async def terminate(self, task_id: str, *, on_event: Any = None) -> Any:
        del on_event
        return await self.provider.terminate(task_id)

    def read_state(self, task_id: str) -> Any:
        return self.provider.read_state(task_id)

    def read_publication_state(self, task_id: str) -> dict[str, Any]:
        reader = getattr(self.provider, "read_publication_state", None)
        if callable(reader):
            value = reader(task_id)
            return value if isinstance(value, dict) else {}
        # Older/mock providers do not expose the raw state; the installer will
        # fall back to the canonical task run state file.
        return {}

    def read_report(self, task_id: str) -> Any:
        return self.provider.read_report(task_id)

    def get_tree(self, task_id: str) -> Any:
        return self.provider.get_tree(task_id)

    def locate_artifact(self, task_id: str, artifact_id: str | None = None) -> Any:
        return self.provider.locate_artifact(task_id, artifact_id)


def _dataset_result(value: Any) -> RsiDatasetResult:
    """Normalize local/core validation results at the adapter boundary."""

    raw = _plain(value)
    if not isinstance(raw, dict):
        raise RsiNotReady("HarnessProvider 返回了无法识别的校验结果")
    errors: list[dict[str, str]] = []
    for item in raw.get("errors") or []:
        item_raw = _plain(item)
        if not isinstance(item_raw, dict):
            continue
        errors.append(
            {
                "reason": str(item_raw.get("message") or item_raw.get("reason") or "输入校验失败"),
                "code": str(item_raw.get("code") or "DATASET_INVALID"),
            }
        )
    sample_count = raw.get("sample_count")
    if sample_count is not None:
        try:
            sample_count = int(sample_count)
        except (TypeError, ValueError):
            sample_count = None
    return RsiDatasetResult(
        valid=bool(raw.get("valid")),
        sample_count=sample_count,
        errors=errors,
    )


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "HarnessEngineAdapter",
    "HarnessEngineRequest",
    "HarnessProviderContract",
]
