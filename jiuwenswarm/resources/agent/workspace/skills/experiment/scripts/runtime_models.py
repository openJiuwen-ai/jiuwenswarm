"""Internal runtime models for the Experiment skill.

These models never cross the module-three public boundary.  They describe the
concrete commands and per-seed tasks that turn a planning order into real runs.
"""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


_SENSITIVE_NAMES = {
    "TOKEN",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "SECRET",
    "CLIENT_SECRET",
    "PASSWORD",
    "API_KEY",
    "PRIVATE_KEY",
}


def _is_sensitive_name(value: str) -> bool:
    normalized = value.upper().replace("-", "_").lstrip("_")
    return normalized in _SENSITIVE_NAMES or any(
        marker in normalized
        for marker in (
            "TOKEN",
            "SECRET",
            "PASSWORD",
            "API_KEY",
            "APIKEY",
            "PRIVATE_KEY",
            "ACCESS_KEY",
            "CREDENTIAL",
        )
    )


def _secret_like_keys(values: dict[str, str]) -> list[str]:
    return [key for key in values if _is_sensitive_name(key)]


def _command_contains_inline_secret(command: list[str]) -> bool:
    for index, argument in enumerate(command):
        option = argument.split("=", 1)[0]
        if not _is_sensitive_name(option):
            continue
        if "=" in argument and argument.split("=", 1)[1]:
            return True
        if index + 1 < len(command) and not command[index + 1].startswith("-"):
            return True
    return False


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class ExperimentType(str, Enum):
    PRIMARY = "PRIMARY"
    BASELINE = "BASELINE"
    ABLATION = "ABLATION"


class MetricDirection(str, Enum):
    MAXIMIZE = "MAXIMIZE"
    MINIMIZE = "MINIMIZE"


class SeedAggregation(str, Enum):
    MEAN = "MEAN"


class PartialFileState(RuntimeModel):
    """单个文件已落盘的续传状态（`prepare_data` 写，下次调用据此跳过或续传）。"""

    filename: str = Field(min_length=1)
    bytes_done: int = Field(ge=0)
    url: str = Field(min_length=1)
    etag: str | None = None
    content_length: int | None = None

    @field_validator("url")
    @classmethod
    def url_is_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("partial state url must be http(s)")
        return value


class DownloadProgress(RuntimeModel):
    """下载**未完成但可续跑**时的进度回报。

    为什么需要它（2026-09-17 静态审查 F-06）：宿主对单次工具调用有 300 秒硬超时，
    而且把 `experiment_prepare_data` 判为**非幂等、超时不重试**——一次大下载被硬杀
    就等于整个 task loop 结束。所以工具必须在 300 秒**之内主动返回**，并把"还没下完"
    如实告诉 agent，让它用同一组参数再来一次；而不是被硬杀。

    ⚠️ 这个状态**不是终态**：stage 保持 `INITIALIZED`（可重入），不能走 REPLAN
    （那是终态，会把整轮实验判死）。
    """

    status: Literal["IN_PROGRESS", "COMPLETE", "FAILED"] = "IN_PROGRESS"
    dataset_name: str = Field(min_length=1)
    completed_files: list[str] = Field(default_factory=list)
    pending_files: list[str] = Field(default_factory=list)
    total_files: int = Field(ge=0)
    bytes_on_disk: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    time_budget_seconds: float = Field(gt=0)
    resume_hint: str = Field(min_length=1)


class MetricDefinition(RuntimeModel):
    verified: bool = False
    definition_used: str = Field(min_length=1)
    implementation: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    library: str | None = None
    library_version: str | None = None
    direction: MetricDirection
    unit: str | None = None
    aggregation: str = Field(min_length=1)
    seed_aggregation: SeedAggregation | None = None


class ImplementationDefinition(RuntimeModel):
    ready: bool = False
    planning_aliases: list[str] = Field(default_factory=list)
    experiment_ids: list[str] = Field(default_factory=list)
    command: list[str] = Field(min_length=1)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    required_env: list[str] = Field(default_factory=list)
    metrics_path: str = "raw_results/{run_record_id}/metrics.json"
    uses_gpu: bool = False
    implementation_url: HttpUrl | None = None
    revision: str | None = None
    source_kind: str = Field(
        default="EXISTING",
        pattern="^(EXISTING|BUNDLED|OPEN_SOURCE|GENERATED|CUSTOM)$",
    )
    license: str | None = None
    dependency_plan: list[str] = Field(default_factory=list)
    smoke_test_command: list[str] = Field(default_factory=list)
    smoke_test_passed: bool = False
    notes: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_secret_values(self) -> "ImplementationDefinition":
        unsafe_keys = _secret_like_keys(self.env)
        if unsafe_keys:
            raise ValueError(
                "secret-like variables must be listed in required_env instead of env: "
                f"{sorted(unsafe_keys)}"
            )
        if any(not part.strip() for part in self.command):
            raise ValueError("implementation command arguments cannot be empty")
        if _command_contains_inline_secret(self.command):
            raise ValueError("secret values cannot be passed in command arguments")
        if _command_contains_inline_secret(self.smoke_test_command):
            raise ValueError("secret values cannot be passed in smoke-test arguments")
        if self.ready and not self.smoke_test_passed:
            raise ValueError(
                "ready=true requires smoke_test_passed=true"
            )
        return self


class DatasetPreparationDefinition(RuntimeModel):
    ready: bool = False
    # scaffold_manifest 会把这两个键写进 dataset_preparations 条目（与
    # ImplementationDefinition 同名同义），但本模型此前没有声明它们，导致
    # 非 identity 预处理流水线下 scaffold 出的 manifest 被自己的模型判
    # extra_forbidden，整轮实验在 agent-init 处 fail-closed。
    planning_aliases: list[str] = Field(default_factory=list)
    experiment_ids: list[str] = Field(default_factory=list)
    command: list[str] = Field(min_length=1)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    required_env: list[str] = Field(default_factory=list)
    marker_path: str = "data/{dataset}/.prepared.json"
    notes: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_secret_values(self) -> "DatasetPreparationDefinition":
        unsafe_keys = _secret_like_keys(self.env)
        if unsafe_keys:
            raise ValueError(
                "secret-like variables must be listed in required_env instead of env: "
                f"{sorted(unsafe_keys)}"
            )
        if any(not part.strip() for part in self.command):
            raise ValueError("dataset preparation command arguments cannot be empty")
        if _command_contains_inline_secret(self.command):
            raise ValueError("secret values cannot be passed in command arguments")
        return self


class AnalysisExtensionDefinition(RuntimeModel):
    ready: bool = False
    required: bool = True
    command: list[str] = Field(min_length=1)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    required_env: list[str] = Field(default_factory=list)
    output_path: str = "analysis/extensions/{extension}.json"
    notes: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_secret_values(self) -> "AnalysisExtensionDefinition":
        unsafe_keys = _secret_like_keys(self.env)
        if unsafe_keys:
            raise ValueError(
                "secret-like variables must be listed in required_env instead of env: "
                f"{sorted(unsafe_keys)}"
            )
        if any(not part.strip() for part in self.command):
            raise ValueError("analysis extension command arguments cannot be empty")
        if _command_contains_inline_secret(self.command):
            raise ValueError("secret values cannot be passed in command arguments")
        return self


class ImplementationManifest(RuntimeModel):
    schema_version: str = Field(pattern=r"^1\.\d+\.\d+$")
    execution_approved: bool = False
    approval_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    approved_by: str | None = None
    approved_at_utc: str | None = None
    approval_type: str | None = Field(default=None, pattern="^LLM_REVIEW$")
    reviewer_model: str | None = None
    review_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_code_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    reviewed_smoke_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    datasets: dict[str, str] = Field(default_factory=dict)
    dataset_preparations: dict[str, DatasetPreparationDefinition] = Field(
        default_factory=dict
    )
    analysis_extensions: dict[str, AnalysisExtensionDefinition] = Field(
        default_factory=dict
    )
    implementations: dict[str, ImplementationDefinition] = Field(min_length=1)
    metrics: dict[str, MetricDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def planning_aliases_are_unambiguous(self) -> "ImplementationManifest":
        owners: dict[str, str] = {}
        collisions: list[str] = []
        for name, definition in self.implementations.items():
            for alias in [name, *definition.planning_aliases]:
                normalized = alias.strip().casefold()
                if not normalized:
                    raise ValueError(
                        f"implementation {name} contains a blank planning alias"
                    )
                previous = owners.setdefault(normalized, name)
                if previous != name:
                    collisions.append(alias)
        if collisions:
            raise ValueError(
                "implementation planning aliases must resolve uniquely: "
                f"{sorted(set(collisions))}"
            )
        if not self.execution_approved and any(
            value is not None
            for value in (
                self.approval_digest,
                self.approved_by,
                self.approved_at_utc,
                self.approval_type,
                self.reviewer_model,
                self.review_digest,
                self.reviewed_code_digest,
                self.reviewed_smoke_digest,
            )
        ):
            raise ValueError(
                "approval metadata is forbidden when execution_approved=false"
            )
        if self.execution_approved:
            # Legacy manifests are still parseable for Module 2 adaptation and
            # deterministic CLI compatibility.  They are never sufficient for
            # Agent execution: _verify_execution_approval requires the full
            # LLM_REVIEW evidence chain persisted by the coordinator.
            if self.approval_type is None:
                legacy_review_fields = (
                    self.reviewer_model,
                    self.review_digest,
                    self.reviewed_code_digest,
                    self.reviewed_smoke_digest,
                )
                if not self.approved_by or any(legacy_review_fields):
                    raise ValueError(
                        "legacy execution approval requires approved_by and forbids partial LLM metadata"
                    )
                return self
            required = {
                "approval_digest": self.approval_digest,
                "approved_by": self.approved_by,
                "approved_at_utc": self.approved_at_utc,
                "approval_type": self.approval_type,
                "reviewer_model": self.reviewer_model,
                "review_digest": self.review_digest,
                "reviewed_code_digest": self.reviewed_code_digest,
                "reviewed_smoke_digest": self.reviewed_smoke_digest,
            }
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError(
                    "execution_approved=true requires LLM review metadata: "
                    + ", ".join(missing)
                )
            if self.approval_type != "LLM_REVIEW":
                raise ValueError("execution approval must use approval_type=LLM_REVIEW")
            if self.approved_by != "experiment-agent":
                raise ValueError(
                    "LLM review approval must be issued by experiment-agent"
                )
        return self


class ExecutionSpec(RuntimeModel):
    experiment_id: str = Field(min_length=1)
    experiment_type: ExperimentType
    hypothesis_ids: list[str] = Field(default_factory=list)
    dataset: str = Field(min_length=1)
    dataset_path: str = Field(min_length=1)
    method: str = Field(min_length=1)
    metric_names: list[str] = Field(min_length=1)
    seed: int
    run_record_id: str = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)
    split_strategy: dict[str, Any]
    preprocessing_pipeline: list[str] = Field(min_length=1)


class ParsedMetric(RuntimeModel):
    name: str = Field(min_length=1)
    value: float
    unit: str | None = None
    split: str = Field(default="test", pattern="^(train|val|test)$")


class RuntimeFileEvidence(RuntimeModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def path_is_run_relative(cls, value: str) -> str:
        windows = PureWindowsPath(value)
        posix = PurePosixPath(value.replace("\\", "/"))
        if (
            windows.drive
            or windows.is_absolute()
            or posix.is_absolute()
            or ".." in posix.parts
        ):
            raise ValueError("runtime evidence path must be run_dir-relative")
        return posix.as_posix()


class RuntimeRunResult(RuntimeModel):
    run_record_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    success: bool
    attempts: int = Field(ge=1)
    duration_seconds: float = Field(ge=0)
    uses_gpu: bool
    command: str = Field(min_length=1)
    config_path: str = Field(min_length=1)
    log_path: str = Field(min_length=1)
    metrics: list[ParsedMetric] = Field(default_factory=list)
    evidence_files: list[RuntimeFileEvidence] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def validate_success_evidence(self) -> "RuntimeRunResult":
        if self.success:
            if self.error is not None or not self.metrics:
                raise ValueError("successful runtime result requires metrics and forbids error")
        elif not self.error or self.metrics:
            raise ValueError("failed runtime result requires error and forbids metrics")
        return self


class AggregateRecord(RuntimeModel):
    experiment_id: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    method: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)
    direction: MetricDirection
    unit: str | None = None
    count: int = Field(gt=0)
    mean: float
    std: float
    minimum: float
    maximum: float
    ci95_low: float
    ci95_high: float
    source_experiment_ids: list[str] = Field(min_length=1)
    source_metric_record_ids: list[str] = Field(min_length=1)


class StageBlocker(RuntimeModel):
    reason: str = Field(min_length=1)
    affected_experiment_ids: list[str] = Field(min_length=1)
    blockers: list[str] = Field(min_length=1)
    suggested_changes: list[str] = Field(min_length=1)
