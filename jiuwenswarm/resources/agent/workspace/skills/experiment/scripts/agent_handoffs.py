"""Strict, persisted handoff contracts for the Experiment agent hierarchy."""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts import PlanningFeedback
from runtime_models import DownloadProgress


def _relative_artifact(value: str) -> str:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if (
        windows.drive
        or windows.is_absolute()
        or posix.is_absolute()
        or ".." in posix.parts
    ):
        raise ValueError("artifact path must be run_dir-relative and cannot contain '..'")
    return posix.as_posix()


class HandoffModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class AgentRunStage(str, Enum):
    INITIALIZED = "INITIALIZED"
    DATA_READY = "DATA_READY"
    CODE_REVIEW_REQUIRED = "CODE_REVIEW_REQUIRED"
    EXECUTION_REVIEW_REQUIRED = "EXECUTION_REVIEW_REQUIRED"
    IMPLEMENTATION_READY = "IMPLEMENTATION_READY"
    EXECUTION_READY = "EXECUTION_READY"
    COMPLETED = "COMPLETED"
    REPLAN = "REPLAN"
    FAILED = "FAILED"


class AgentName(str, Enum):
    ROOT = "experiment-agent"
    DATA = "experiment-data-agent"
    IMPLEMENTATION = "experiment-implementation-agent"
    EXECUTION = "experiment-execution-agent"
    ANALYSIS = "experiment-analysis-agent"
    MODULE_FOUR = "module-four"
    MODULE_TWO = "module-two"


_TERMINAL_STAGES = {
    AgentRunStage.COMPLETED.value,
    AgentRunStage.REPLAN.value,
    AgentRunStage.FAILED.value,
}

_ALLOWED_TRANSITIONS: dict[str | None, set[str]] = {
    None: {
        AgentRunStage.INITIALIZED.value,
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    },
    AgentRunStage.INITIALIZED.value: {
        AgentRunStage.DATA_READY.value,
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    },
    AgentRunStage.DATA_READY.value: {
        AgentRunStage.CODE_REVIEW_REQUIRED.value,
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    },
    AgentRunStage.CODE_REVIEW_REQUIRED.value: {
        AgentRunStage.DATA_READY.value,
        AgentRunStage.EXECUTION_REVIEW_REQUIRED.value,
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    },
    AgentRunStage.EXECUTION_REVIEW_REQUIRED.value: {
        AgentRunStage.DATA_READY.value,
        AgentRunStage.CODE_REVIEW_REQUIRED.value,
        AgentRunStage.IMPLEMENTATION_READY.value,
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    },
    AgentRunStage.IMPLEMENTATION_READY.value: {
        AgentRunStage.CODE_REVIEW_REQUIRED.value,
        AgentRunStage.EXECUTION_READY.value,
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    },
    AgentRunStage.EXECUTION_READY.value: {
        AgentRunStage.COMPLETED.value,
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    },
}


def _expected_consumer(stage: str) -> str:
    consumers = {
        AgentRunStage.INITIALIZED.value: AgentName.DATA.value,
        AgentRunStage.DATA_READY.value: AgentName.IMPLEMENTATION.value,
        AgentRunStage.CODE_REVIEW_REQUIRED.value: AgentName.ROOT.value,
        AgentRunStage.EXECUTION_REVIEW_REQUIRED.value: AgentName.ROOT.value,
        AgentRunStage.IMPLEMENTATION_READY.value: AgentName.EXECUTION.value,
        AgentRunStage.EXECUTION_READY.value: AgentName.ANALYSIS.value,
        AgentRunStage.COMPLETED.value: AgentName.MODULE_FOUR.value,
        AgentRunStage.REPLAN.value: AgentName.MODULE_TWO.value,
        AgentRunStage.FAILED.value: AgentName.ROOT.value,
    }
    return consumers[stage]


class AgentTransitionRecord(HandoffModel):
    sequence: int = Field(ge=1)
    from_stage: AgentRunStage | None
    to_stage: AgentRunStage
    actor: AgentName
    summary: str = Field(min_length=1)
    artifact_paths: list[str] = Field(default_factory=list)

    _paths_are_relative = field_validator("artifact_paths")(
        lambda values: [_relative_artifact(value) for value in values]
    )


class ExperimentAgentState(HandoffModel):
    contract_version: str = Field(default="1.0.0", pattern=r"^1\.\d+\.\d+$")
    run_id: str = Field(min_length=1)
    current_stage: AgentRunStage
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_path: str = Field(min_length=1)
    manifest_path: str | None = None
    dataset_paths: dict[str, str] = Field(default_factory=dict)
    datasets_index_path: str | None = None
    datasets_index_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    implementation_review_path: str | None = None
    implementation_review_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    code_review_context_path: str | None = None
    code_review_context_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    code_review_path: str | None = None
    code_review_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    reviewed_code_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    smoke_results_path: str | None = None
    smoke_results_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    execution_review_context_path: str | None = None
    execution_review_context_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    execution_review_path: str | None = None
    execution_review_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    implementation_approval_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    implementation_inspection_path: str | None = None
    implementation_inspection_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    execution_specs_path: str | None = None
    execution_specs_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    environment_deployment_path: str | None = None
    environment_deployment_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    runtime_results_path: str | None = None
    runtime_results_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    output_path: str | None = None
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_manifest_path: str | None = None
    artifact_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    warnings: list[str] = Field(default_factory=list)
    started_at_epoch: float = Field(ge=0)
    transitions: list[AgentTransitionRecord] = Field(min_length=1)

    _request_path_is_relative = field_validator("request_path")(_relative_artifact)
    _dataset_paths_are_relative = field_validator("dataset_paths")(
        lambda value: {name: _relative_artifact(path) for name, path in value.items()}
    )

    @field_validator(
        "manifest_path",
        "datasets_index_path",
        "implementation_review_path",
        "code_review_context_path",
        "code_review_path",
        "smoke_results_path",
        "execution_review_context_path",
        "execution_review_path",
        "implementation_inspection_path",
        "execution_specs_path",
        "environment_deployment_path",
        "runtime_results_path",
        "output_path",
        "artifact_manifest_path",
    )
    @classmethod
    def optional_paths_are_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact(value)

    @model_validator(mode="after")
    def validate_transition_trace(self) -> "ExperimentAgentState":
        expected_from: str | None = None
        for index, transition in enumerate(self.transitions, start=1):
            if transition.sequence != index:
                raise ValueError("transition sequence must be contiguous and one-based")
            if transition.from_stage != expected_from:
                raise ValueError("transition trace is discontinuous")
            if transition.to_stage not in _ALLOWED_TRANSITIONS.get(
                expected_from, set()
            ):
                raise ValueError(
                    "transition trace contains an illegal state change: "
                    f"{expected_from} -> {transition.to_stage}"
                )
            expected_from = transition.to_stage
        if expected_from != self.current_stage:
            raise ValueError("current_stage must equal the final transition target")
        visited = {item.to_stage for item in self.transitions}
        if AgentRunStage.DATA_READY.value in visited and not self.dataset_paths:
            raise ValueError("DATA_READY transition requires dataset_paths")
        if AgentRunStage.DATA_READY.value in visited and (
            self.datasets_index_path is None
            or self.datasets_index_digest is None
        ):
            raise ValueError("DATA_READY transition requires dataset index path/digest")
        if (
            AgentRunStage.CODE_REVIEW_REQUIRED.value in visited
            and (
                self.implementation_review_path is None
                or self.implementation_review_digest is None
                or self.code_review_context_path is None
                or self.code_review_context_digest is None
            )
        ):
            raise ValueError(
                "CODE_REVIEW_REQUIRED requires deterministic and isolated review materials"
            )
        if self.current_stage in {
            AgentRunStage.EXECUTION_REVIEW_REQUIRED.value,
            AgentRunStage.IMPLEMENTATION_READY.value,
            AgentRunStage.EXECUTION_READY.value,
            AgentRunStage.COMPLETED.value,
        } and (
            self.code_review_path is None
            or self.code_review_digest is None
            or self.reviewed_code_digest is None
            or self.smoke_results_path is None
            or self.smoke_results_digest is None
            or self.execution_review_context_path is None
            or self.execution_review_context_digest is None
        ):
            raise ValueError(
                "EXECUTION_REVIEW_REQUIRED requires code review and smoke evidence"
            )
        if (
            self.current_stage
            in {
                AgentRunStage.IMPLEMENTATION_READY.value,
                AgentRunStage.EXECUTION_READY.value,
                AgentRunStage.COMPLETED.value,
            }
            and (
                self.execution_specs_path is None
                or self.execution_specs_digest is None
                or self.implementation_approval_digest is None
                or self.environment_deployment_path is None
                or self.environment_deployment_digest is None
            )
        ):
            raise ValueError(
                "IMPLEMENTATION_READY requires specs, deployed environment and bound approval"
            )
        if (
            AgentRunStage.EXECUTION_READY.value in visited
            and (
                self.runtime_results_path is None
                or self.runtime_results_digest is None
            )
        ):
            raise ValueError(
                "EXECUTION_READY transition requires runtime results path/digest"
            )
        if self.current_stage in _TERMINAL_STAGES and (
            self.output_path is None or self.output_digest is None
        ):
            raise ValueError("terminal Agent state requires output path/digest")
        if self.current_stage == AgentRunStage.COMPLETED.value and (
            self.artifact_manifest_path is None
            or self.artifact_manifest_digest is None
        ):
            raise ValueError(
                "COMPLETED Agent state requires artifact manifest path/digest"
            )
        if bool(self.execution_specs_path) != bool(self.execution_specs_digest):
            raise ValueError("execution specs path and digest must appear together")
        if bool(self.runtime_results_path) != bool(self.runtime_results_digest):
            raise ValueError("runtime results path and digest must appear together")
        if bool(self.environment_deployment_path) != bool(
            self.environment_deployment_digest
        ):
            raise ValueError(
                "environment deployment path and digest must appear together"
            )
        if bool(self.output_path) != bool(self.output_digest):
            raise ValueError("output path and digest must appear together")
        if bool(self.datasets_index_path) != bool(self.datasets_index_digest):
            raise ValueError("dataset index path and digest must appear together")
        if bool(self.implementation_review_path) != bool(
            self.implementation_review_digest
        ):
            raise ValueError("implementation review path and digest must appear together")
        if bool(self.implementation_inspection_path) != bool(
            self.implementation_inspection_digest
        ):
            raise ValueError(
                "implementation inspection path and digest must appear together"
            )
        for label, path, digest in (
            ("code review context", self.code_review_context_path, self.code_review_context_digest),
            ("code review", self.code_review_path, self.code_review_digest),
            ("smoke results", self.smoke_results_path, self.smoke_results_digest),
            (
                "execution review context",
                self.execution_review_context_path,
                self.execution_review_context_digest,
            ),
            ("execution review", self.execution_review_path, self.execution_review_digest),
        ):
            if bool(path) != bool(digest):
                raise ValueError(f"{label} path and digest must appear together")
        if bool(self.artifact_manifest_path) != bool(
            self.artifact_manifest_digest
        ):
            raise ValueError("artifact manifest path and digest must appear together")
        return self


class StageRequest(HandoffModel):
    contract_version: str = Field(default="1.0.0", pattern=r"^1\.\d+\.\d+$")
    run_id: str = Field(min_length=1)
    state_path: str = "outputs/agent-state.json"

    _state_path_is_relative = field_validator("state_path")(_relative_artifact)


class InitializeResponse(HandoffModel):
    contract_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    run_dir: str = Field(min_length=1)
    stage: AgentRunStage
    terminal: bool
    state_path: str = "outputs/agent-state.json"
    next_agent: AgentName | None
    summary: str = Field(min_length=1)
    output_path: str | None = None
    artifact_manifest_path: str | None = None
    planning_feedback: PlanningFeedback | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    execution_monitor_path: str | None = None
    execution_progress: dict[str, object] | None = None

    _state_path_is_relative = field_validator("state_path")(_relative_artifact)

    @field_validator(
        "output_path", "artifact_manifest_path", "execution_monitor_path"
    )
    @classmethod
    def output_path_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact(value)

    @model_validator(mode="after")
    def validate_route(self) -> "InitializeResponse":
        if self.terminal != (self.stage in _TERMINAL_STAGES):
            raise ValueError("terminal flag does not match stage")
        expected = {
            AgentRunStage.INITIALIZED.value: AgentName.DATA.value,
            AgentRunStage.DATA_READY.value: AgentName.IMPLEMENTATION.value,
            AgentRunStage.CODE_REVIEW_REQUIRED.value: AgentName.ROOT.value,
            AgentRunStage.EXECUTION_REVIEW_REQUIRED.value: AgentName.ROOT.value,
            AgentRunStage.IMPLEMENTATION_READY.value: AgentName.EXECUTION.value,
            AgentRunStage.EXECUTION_READY.value: AgentName.ANALYSIS.value,
            AgentRunStage.COMPLETED.value: AgentName.MODULE_FOUR.value,
            AgentRunStage.REPLAN.value: AgentName.MODULE_TWO.value,
            AgentRunStage.FAILED.value: None,
        }[self.stage]
        if self.next_agent != expected:
            raise ValueError("next_agent does not match stage")
        if self.terminal and self.output_path is None:
            raise ValueError("terminal response requires output_path")
        if self.stage == AgentRunStage.COMPLETED.value:
            if self.artifact_manifest_path is None:
                raise ValueError("COMPLETED response requires artifact_manifest_path")
        elif self.artifact_manifest_path is not None:
            raise ValueError(
                "non-COMPLETED response cannot claim an artifact manifest"
            )
        _validate_terminal_details(
            self.stage, self.planning_feedback, self.errors
        )
        return self


class DataAgentResponse(HandoffModel):
    contract_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    run_dir: str = Field(min_length=1)
    producer: AgentName = AgentName.DATA
    consumer: AgentName
    stage: AgentRunStage
    terminal: bool
    prepared_dataset_count: int = Field(ge=0)
    datasets_index_path: str | None = None
    state_path: str = "outputs/agent-state.json"
    summary: str = Field(min_length=1)
    planning_feedback: PlanningFeedback | None = None
    errors: list[str] = Field(default_factory=list)
    #: 下载未完成但可续跑时的进度（见 runtime_models.DownloadProgress）。
    #: 非 None 时 `terminal=False` 且 stage 停在 INITIALIZED——agent 应原样再调一次，
    #: 而不是把它当成失败去 REPLAN。
    download_progress: DownloadProgress | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("datasets_index_path")
    @classmethod
    def dataset_index_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact(value)

    _state_path_is_relative = field_validator("state_path")(_relative_artifact)

    @model_validator(mode="after")
    def validate_handoff(self) -> "DataAgentResponse":
        _validate_common_response(self.stage, self.terminal, self.consumer)
        _validate_terminal_details(
            self.stage, self.planning_feedback, self.errors
        )
        if bool(self.datasets_index_path) != bool(self.prepared_dataset_count):
            raise ValueError(
                "datasets_index_path and prepared_dataset_count must agree"
            )
        return self


class ImplementationAgentResponse(HandoffModel):
    contract_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    run_dir: str = Field(min_length=1)
    producer: AgentName = AgentName.IMPLEMENTATION
    consumer: AgentName
    stage: AgentRunStage
    terminal: bool
    execution_spec_count: int = Field(ge=0)
    execution_specs_path: str | None = None
    implementation_review_path: str | None = None
    implementation_inspection_path: str | None = None
    inspection: dict | None = None
    approval_required: bool = False
    review_phase: str | None = Field(
        default=None,
        pattern="^(CODE_REVIEW_REQUIRED|EXECUTION_REVIEW_REQUIRED)$",
    )
    state_path: str = "outputs/agent-state.json"
    summary: str = Field(min_length=1)
    planning_feedback: PlanningFeedback | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "execution_specs_path",
        "implementation_review_path",
        "implementation_inspection_path",
    )
    @classmethod
    def execution_specs_are_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact(value)

    _state_path_is_relative = field_validator("state_path")(_relative_artifact)

    @model_validator(mode="after")
    def validate_handoff(self) -> "ImplementationAgentResponse":
        _validate_common_response(self.stage, self.terminal, self.consumer)
        _validate_terminal_details(
            self.stage, self.planning_feedback, self.errors
        )
        if bool(self.execution_specs_path) != bool(self.execution_spec_count):
            raise ValueError(
                "execution_specs_path and execution_spec_count must agree"
            )
        if self.approval_required != (
            self.stage
            in {
                AgentRunStage.CODE_REVIEW_REQUIRED.value,
                AgentRunStage.EXECUTION_REVIEW_REQUIRED.value,
            }
        ):
            raise ValueError("approval_required must match review stage")
        if self.approval_required and self.implementation_review_path is None:
            raise ValueError("review stage requires implementation_review_path")
        if bool(self.implementation_inspection_path) != bool(self.inspection):
            raise ValueError("inspection path and payload must appear together")
        expected_phase = self.stage if self.approval_required else None
        if self.review_phase != expected_phase:
            raise ValueError("review_phase must identify the active root review stage")
        return self


class ExecutionAgentResponse(HandoffModel):
    contract_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    run_dir: str = Field(min_length=1)
    producer: AgentName = AgentName.EXECUTION
    consumer: AgentName
    stage: AgentRunStage
    terminal: bool
    successful_run_count: int = Field(ge=0)
    failed_run_count: int = Field(ge=0)
    runtime_results_path: str | None = None
    execution_monitor_path: str | None = None
    execution_events_path: str | None = None
    execution_progress: dict[str, object] | None = None
    state_path: str = "outputs/agent-state.json"
    summary: str = Field(min_length=1)
    planning_feedback: PlanningFeedback | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "runtime_results_path", "execution_monitor_path", "execution_events_path"
    )
    @classmethod
    def runtime_results_are_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact(value)

    _state_path_is_relative = field_validator("state_path")(_relative_artifact)

    @model_validator(mode="after")
    def validate_handoff(self) -> "ExecutionAgentResponse":
        _validate_common_response(self.stage, self.terminal, self.consumer)
        _validate_terminal_details(
            self.stage, self.planning_feedback, self.errors
        )
        if (
            self.successful_run_count + self.failed_run_count > 0
            and self.runtime_results_path is None
        ):
            raise ValueError("recorded runs require runtime_results_path")
        return self


class RootReviewResponse(HandoffModel):
    contract_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    run_dir: str = Field(min_length=1)
    producer: AgentName = AgentName.ROOT
    consumer: AgentName
    stage: AgentRunStage
    terminal: bool
    review_phase: str | None = Field(
        default=None,
        pattern="^(CODE_REVIEW_REQUIRED|EXECUTION_REVIEW_REQUIRED)$",
    )
    review_context_path: str | None = None
    review_context: dict[str, object] | None = None
    summary: str = Field(min_length=1)
    planning_feedback: PlanningFeedback | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("review_context_path")
    @classmethod
    def context_path_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact(value)

    @model_validator(mode="after")
    def validate_review_response(self) -> "RootReviewResponse":
        _validate_common_response(self.stage, self.terminal, self.consumer)
        _validate_terminal_details(self.stage, self.planning_feedback, self.errors)
        expected_phase = (
            self.stage
            if self.stage
            in {
                AgentRunStage.CODE_REVIEW_REQUIRED.value,
                AgentRunStage.EXECUTION_REVIEW_REQUIRED.value,
            }
            else None
        )
        if self.review_phase != expected_phase:
            raise ValueError("review_phase must match the active review stage")
        if bool(self.review_context_path) != bool(self.review_context):
            raise ValueError("review context path and payload must appear together")
        if expected_phase is not None and self.review_context_path is None:
            raise ValueError("review stage requires an isolated context")
        return self


class AnalysisAgentResponse(HandoffModel):
    contract_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    run_dir: str = Field(min_length=1)
    producer: AgentName = AgentName.ANALYSIS
    consumer: AgentName
    stage: AgentRunStage
    terminal: bool
    module_status: str | None = Field(default=None, pattern="^(PASS|PARTIAL|REPLAN|FAILED)$")
    output_path: str | None = None
    artifact_manifest_path: str | None = None
    visualization_candidate_count: int = Field(ge=0)
    state_path: str = "outputs/agent-state.json"
    summary: str = Field(min_length=1)
    planning_feedback: PlanningFeedback | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("output_path", "artifact_manifest_path")
    @classmethod
    def output_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact(value)

    _state_path_is_relative = field_validator("state_path")(_relative_artifact)

    @model_validator(mode="after")
    def validate_handoff(self) -> "AnalysisAgentResponse":
        _validate_common_response(self.stage, self.terminal, self.consumer)
        _validate_terminal_details(
            self.stage, self.planning_feedback, self.errors
        )
        if self.terminal:
            if self.output_path is None or self.module_status is None:
                raise ValueError(
                    "terminal analysis response requires module_status and output_path"
                )
        elif self.output_path is not None or self.module_status is not None:
            raise ValueError(
                "non-terminal analysis response cannot claim a module output"
            )
        if self.stage == AgentRunStage.COMPLETED.value:
            if self.artifact_manifest_path is None:
                raise ValueError(
                    "COMPLETED analysis response requires artifact_manifest_path"
                )
        elif self.artifact_manifest_path is not None:
            raise ValueError(
                "non-COMPLETED analysis response cannot claim an artifact manifest"
            )
        return self


def _validate_common_response(
    stage: str,
    terminal: bool,
    consumer: str,
) -> None:
    if terminal != (stage in _TERMINAL_STAGES):
        raise ValueError("terminal flag does not match stage")
    if consumer != _expected_consumer(stage):
        raise ValueError("consumer does not match stage")


def _validate_terminal_details(
    stage: str,
    planning_feedback: PlanningFeedback | None,
    errors: list[str],
) -> None:
    if stage == AgentRunStage.REPLAN.value:
        if planning_feedback is None or errors:
            raise ValueError("REPLAN response requires planning_feedback and forbids errors")
    elif stage == AgentRunStage.FAILED.value:
        if not errors or planning_feedback is not None:
            raise ValueError("FAILED response requires errors and forbids planning_feedback")
    elif planning_feedback is not None or errors:
        raise ValueError(
            "non-REPLAN/FAILED response forbids planning_feedback and errors"
        )
