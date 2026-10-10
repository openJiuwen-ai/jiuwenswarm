"""Resumable stage coordinator used by the ExperimentAgent and its subagents."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from agent_handoffs import (
    AgentName,
    AgentRunStage,
    AgentTransitionRecord,
    AnalysisAgentResponse,
    DataAgentResponse,
    ExecutionAgentResponse,
    ExperimentAgentState,
    ImplementationAgentResponse,
    InitializeResponse,
    RootReviewResponse,
)
from analyze_results import (
    aggregate_metrics,
    build_analysis_records,
    build_findings,
    build_head_statistics,
    evaluate_criteria,
    evaluate_hypotheses,
    materialize_execution_records,
)
from build_visualizations import build_visualization_package
from check_feasibility import check_feasibility
from environment_manager import (
    DEPLOYMENT_PATH,
    EnvironmentDeploymentError,
    load_execution_python,
    prepare_execution_environment,
)
from execution_monitor import EVENTS_PATH, MONITOR_PATH
from contracts import (
    BaselineImplementation,
    ExperimentModuleInput,
    ExperimentModuleOutput,
    ExperimentResults,
    MetricImplementation,
    PlanningFeedback,
    Reproducibility,
    ResourceUsage,
)
from io_utils import (
    portable_arguments,
    portable_command,
    read_json,
    relative_to_run,
    resolve_run_dir,
    safe_join,
    slugify,
    write_json_atomic,
)
from implementation_approval import (
    implementation_approval_digest,
    implementation_code_digest,
)
from implementation_builder import (
    GeneratedImplementationProposal,
    ImplementationInspection,
    ImplementationReview,
    _standard_family,
    build_implementations,
    inspect_implementation_context,
    write_generated_implementation,
)
from load_inputs import default_manifest_path, load_manifest, validate_request
from plan_execution import build_execution_specs
from prepare_data import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    dataset_fingerprint,
    prepare_datasets,
)
from run_analysis_extensions import run_analysis_extensions, write_analysis_context
from run_experiments import execute_all, execute_one
from runtime_models import (
    DownloadProgress,
    ExecutionSpec,
    ImplementationManifest,
    RuntimeFileEvidence,
    RuntimeRunResult,
    StageBlocker,
)
from review_contracts import (
    CodeReviewContext,
    CodeReviewDecision,
    ExecutionReviewContext,
    ExecutionReviewDecision,
    build_code_review_context,
    deterministic_static_review,
    review_decision_digest,
)
from write_outputs import (
    reproducibility_command,
    snapshot_inputs,
    portable_manifest_payload,
    write_environment,
    write_module_output,
    write_runtime_results,
)


STATE_PATH = "outputs/agent-state.json"

#: 被新 run_id 取代的旧运行状态归档到这里，避免同一 run_dir 的新 run 覆盖旧证据。
PREVIOUS_RUNS_DIR = "previous-runs"

#: run_dir 中属于「本次运行状态」的条目。run_id 变更时归档它们；两处有意排除：
#: - `data/`：下载断点与完成标记必须跨 run 复用，否则每轮 REPLAN 都要重下数 GB。
#: - `implementation-manifest.json`：它是编排层放在 run_dir 里的**输入**（paper-gen
#:   的 `_prepare_agent_inputs` 就把它写到 run_dir 根），归档掉会让新 run 直接
#:   报「缺少实现清单」。它每轮都会被重新写入，不属于上一轮的运行状态。
RUN_STATE_ENTRIES = (
    "inputs",
    "outputs",
    "implementations",
    "environment",
    "logs",
)
EXECUTION_SPECS_PATH = "outputs/execution-specs.json"
RUNTIME_RESULTS_PATH = "outputs/runtime-results.json"
MODULE_OUTPUT_PATH = "outputs/experiment-module-output.json"
ARTIFACT_MANIFEST_PATH = "outputs/artifact-manifest.json"
IMPLEMENTATION_REVIEW_PATH = "outputs/implementation-review.json"
IMPLEMENTATION_INSPECTION_PATH = "outputs/implementation-inspection.json"
CODE_REVIEW_CONTEXT_PATH = "outputs/code-review-context.json"
CODE_REVIEW_PATH = "outputs/code-review.json"
SMOKE_RESULTS_PATH = "outputs/smoke-results.json"
EXECUTION_REVIEW_CONTEXT_PATH = "outputs/execution-review-context.json"
EXECUTION_REVIEW_PATH = "outputs/execution-review.json"
_SMOKE_MAX_DATA_BYTES = 4 * 1024 * 1024
_SMOKE_MAX_DATA_FILES = 32
_SMOKE_MAX_TEXT_LINES = 512
_SMOKE_TEXT_SUFFIXES = {".csv", ".tsv", ".jsonl", ".ndjson", ".txt"}

#: 冒烟测试的时间上限（秒），可用 `JIUWENSWARM_SMOKE_TIMEOUT_SECONDS` 覆盖。
#:
#: 2026-09-17 修：此前硬编码 60 秒。冒烟在 LoCoMo 这类真实数据集上跑全量时，
#: 基线单次就要 47.6–48.1 秒、主方法必然超过 60 秒，于是连续超时把流水线拖进
#: REPLAN——但 REPLAN 只重做规划，改不了这个时间预算，只会空转。冒烟仍需有界，
#: 边界得现实且可调，因此改为可配置、默认 300 秒。
_SMOKE_TIMEOUT_SECONDS = int(
    os.environ.get("JIUWENSWARM_SMOKE_TIMEOUT_SECONDS", "300")
)

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


class ExperimentAgentCoordinator:
    """Persisted state machine shared by CLI mode and Agent tools."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        manifest_path: Path | None = None,
        allow_downloads: bool = True,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        base_dir: Path | None = None,
        time_budget_seconds: float | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self.allow_downloads = allow_downloads
        self.max_download_bytes = max_download_bytes
        self.base_dir = base_dir
        #: 单次 `prepare_data` 调用的时间预算。宿主对工具调用有 300 秒硬超时且
        #: 判为非幂等不重试，所以默认给 240 秒留出收尾余量——工具**自己**在超时前
        #: 返回进度，比被硬杀（整个 task loop 结束）健康得多。None = 不限（CLI 直调）。
        self.time_budget_seconds = time_budget_seconds

    def initialize(
        self,
        payload: ExperimentModuleInput | dict[str, Any],
    ) -> InitializeResponse:
        request = validate_request(payload)
        run_dir = resolve_run_dir(
            request.execution_config.run_dir,
            base_dir=self.base_dir,
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        with _stage_lock(run_dir):
            request_digest = _digest_payload(request.model_dump(mode="json"))
            state_path = safe_join(run_dir, STATE_PATH)
            manifest_source = self.manifest_path or default_manifest_path(
                request,
                base_dir=self.base_dir,
            )
            if not manifest_source.is_absolute():
                manifest_source = ((self.base_dir or Path.cwd()) / manifest_source).resolve()

            if state_path.is_file():
                state = _load_state(run_dir)
                if state.run_id == request.run_id:
                    if state.request_digest != request_digest:
                        raise ValueError(
                            "run_dir already contains a different request for this run_id"
                        )
                    # After initialization the run-local, hashed snapshot is the
                    # authority.  Builder/approval updates are intentionally made
                    # only to that snapshot, never to Module 2 or the source file.
                    return _initialize_response(state, run_dir)
                # Different run identities must never share a mutable state
                # directory.  The paper-gen adapter now allocates
                # ``runs/<run_id>``; retaining this guard protects standalone
                # callers and catches bad integrations instead of silently
                # archiving/overwriting another run.
                raise ValueError("run_dir already belongs to a different run_id")

            request_path = safe_join(run_dir, "inputs/experiment-request.json")
            write_json_atomic(request_path, request.model_dump(mode="json"))
            started_at = time.time()

            if not manifest_source.is_file():
                blocker = StageBlocker(
                    reason="缺少实现清单",
                    affected_experiment_ids=request.experiment_plan.primary_experiments,
                    blockers=[
                        f"找不到implementation manifest: {manifest_source.name}"
                    ],
                    suggested_changes=[
                        "复制templates/implementation-manifest.json到run_dir，填写真实命令和指标定义"
                    ],
                )
                output = _blocked_output(request, blocker, [], started_at)
                write_module_output(output, run_dir)
                state = ExperimentAgentState(
                    run_id=request.run_id,
                    current_stage=AgentRunStage.REPLAN,
                    request_digest=request_digest,
                    manifest_digest=None,
                    request_path=relative_to_run(request_path, run_dir),
                    manifest_path=None,
                    output_path=MODULE_OUTPUT_PATH,
                    output_digest=_digest_file(
                        safe_join(run_dir, MODULE_OUTPUT_PATH)
                    ),
                    warnings=[],
                    started_at_epoch=started_at,
                    transitions=[
                        AgentTransitionRecord(
                            sequence=1,
                            from_stage=None,
                            to_stage=AgentRunStage.REPLAN,
                            actor=AgentName.ROOT,
                            summary=blocker.reason,
                            artifact_paths=[MODULE_OUTPUT_PATH],
                        )
                    ],
                )
                _save_state(state, run_dir)
                return _initialize_response(state, run_dir)

            manifest = load_manifest(manifest_source)
            request_path, manifest_snapshot = snapshot_inputs(request, manifest, run_dir)
            manifest_digest = _digest_payload(read_json(manifest_snapshot))
            warnings, blocker = check_feasibility(request, manifest)
            if blocker is not None:
                output = _blocked_output(request, blocker, warnings, started_at)
                write_module_output(output, run_dir)
                state = ExperimentAgentState(
                    run_id=request.run_id,
                    current_stage=AgentRunStage.REPLAN,
                    request_digest=request_digest,
                    manifest_digest=manifest_digest,
                    request_path=relative_to_run(request_path, run_dir),
                    manifest_path=relative_to_run(manifest_snapshot, run_dir),
                    output_path=MODULE_OUTPUT_PATH,
                    output_digest=_digest_file(
                        safe_join(run_dir, MODULE_OUTPUT_PATH)
                    ),
                    warnings=_deduplicate(warnings),
                    started_at_epoch=started_at,
                    transitions=[
                        AgentTransitionRecord(
                            sequence=1,
                            from_stage=None,
                            to_stage=AgentRunStage.REPLAN,
                            actor=AgentName.ROOT,
                            summary=blocker.reason,
                            artifact_paths=[MODULE_OUTPUT_PATH],
                        )
                    ],
                )
                _save_state(state, run_dir)
                return _initialize_response(state, run_dir)

            state = ExperimentAgentState(
                run_id=request.run_id,
                current_stage=AgentRunStage.INITIALIZED,
                request_digest=request_digest,
                manifest_digest=manifest_digest,
                request_path=relative_to_run(request_path, run_dir),
                manifest_path=relative_to_run(manifest_snapshot, run_dir),
                warnings=_deduplicate(warnings),
                started_at_epoch=started_at,
                transitions=[
                    AgentTransitionRecord(
                        sequence=1,
                        from_stage=None,
                        to_stage=AgentRunStage.INITIALIZED,
                        actor=AgentName.ROOT,
                        summary="输入与资源门禁通过，等待数据阶段后构建和审查实现",
                        artifact_paths=[
                            relative_to_run(request_path, run_dir),
                            relative_to_run(manifest_snapshot, run_dir),
                        ],
                    )
                ],
            )
            _save_state(state, run_dir)
            return _initialize_response(state, run_dir)

    def prepare_data(self, run_dir: Path, *, run_id: str) -> DataAgentResponse:
        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir, expected_run_id=run_id
            )
            if state.current_stage in _TERMINAL_STAGES:
                return _data_response(state, run_dir)
            if state.current_stage != AgentRunStage.INITIALIZED.value:
                if _stage_rank(state.current_stage) > _stage_rank(
                    AgentRunStage.INITIALIZED.value
                ):
                    return _data_response(state, run_dir)
                raise ValueError(
                    f"DataAgent cannot run from stage {state.current_stage}"
                )

            dataset_paths, warnings, blocker, progress = prepare_datasets(
                request,
                manifest,
                run_dir,
                allow_downloads=self.allow_downloads,
                max_download_bytes=self.max_download_bytes,
                time_budget_seconds=self.time_budget_seconds,
            )
            state.warnings = _deduplicate([*state.warnings, *warnings])
            if progress is not None and progress.status == "IN_PROGRESS":
                # ⚠️ **不推进 stage**（保持 INITIALIZED，它是可重入的），也**不走
                # _finish_blocked**（那会把 stage 推到 REPLAN——终态，整轮实验判死）。
                # 宿主对本次工具调用有 300 秒硬超时且判为非幂等不重试，所以工具必须
                # 在超时前主动返回"还没下完"，让 agent 拿同一组参数再来一次。
                _save_state(state, run_dir)
                return _in_progress_response(state, run_dir, progress)
            if blocker is not None:
                _finish_blocked(
                    state,
                    request,
                    run_dir,
                    blocker,
                    actor=AgentName.DATA,
                )
                return _data_response(state, run_dir)

            state.dataset_paths = {
                name: relative_to_run(Path(path), run_dir)
                for name, path in dataset_paths.items()
            }
            state.datasets_index_path = "data/datasets.json"
            state.datasets_index_digest = _digest_file(
                safe_join(run_dir, state.datasets_index_path)
            )
            _transition(
                state,
                AgentRunStage.DATA_READY,
                actor=AgentName.DATA,
                summary=f"已准备{len(dataset_paths)}个数据集",
                artifact_paths=["data/datasets.json"],
            )
            _save_state(state, run_dir)
            return _data_response(state, run_dir)

    def inspect_implementation(
        self,
        run_dir: Path,
        *,
        run_id: str,
    ) -> ImplementationAgentResponse:
        """Materialize the complete read-only context for the Builder LLM."""

        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir, expected_run_id=run_id
            )
            if state.current_stage != AgentRunStage.DATA_READY.value:
                if _stage_rank(state.current_stage) > _stage_rank(
                    AgentRunStage.DATA_READY.value
                ):
                    return _implementation_response(state, run_dir)
                raise ValueError("implementation inspection requires DATA_READY")
            dataset_paths = {
                name: str(safe_join(run_dir, path))
                for name, path in state.dataset_paths.items()
            }
            inspection = inspect_implementation_context(
                request,
                manifest,
                run_dir,
                dataset_paths,
            )
            path = safe_join(run_dir, IMPLEMENTATION_INSPECTION_PATH)
            write_json_atomic(path, inspection.model_dump(mode="json"))
            state.implementation_inspection_path = IMPLEMENTATION_INSPECTION_PATH
            state.implementation_inspection_digest = _digest_file(path)
            _save_state(state, run_dir)
            return _implementation_response(state, run_dir)

    def resolve_implementation(
        self,
        run_dir: Path,
        *,
        run_id: str,
    ) -> ImplementationAgentResponse:
        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir, expected_run_id=run_id
            )
            if state.current_stage in _TERMINAL_STAGES:
                return _implementation_response(state, run_dir)
            if state.current_stage not in {
                AgentRunStage.DATA_READY.value,
            }:
                if _stage_rank(state.current_stage) > _stage_rank(
                    AgentRunStage.CODE_REVIEW_REQUIRED.value
                ):
                    return _implementation_response(state, run_dir)
                raise ValueError(
                    "ImplementationAgent requires DATA_READY; current stage is "
                    f"{state.current_stage}"
                )
            dataset_paths = {
                name: str(safe_join(run_dir, path))
                for name, path in state.dataset_paths.items()
            }
            if state.implementation_inspection_path is None:
                inspection = inspect_implementation_context(
                    request,
                    manifest,
                    run_dir,
                    dataset_paths,
                )
                inspection_path = safe_join(
                    run_dir, IMPLEMENTATION_INSPECTION_PATH
                )
                write_json_atomic(
                    inspection_path, inspection.model_dump(mode="json")
                )
                state.implementation_inspection_path = IMPLEMENTATION_INSPECTION_PATH
                state.implementation_inspection_digest = _digest_file(
                    inspection_path
                )
            updated_manifest, review, blocker = build_implementations(
                request,
                manifest,
                run_dir,
                dataset_paths,
            )
            if blocker is not None:
                _finish_blocked(
                    state,
                    request,
                    run_dir,
                    blocker,
                    actor=AgentName.IMPLEMENTATION,
                )
                return _implementation_response(state, run_dir)
            assert review is not None
            manifest_snapshot = safe_join(run_dir, state.manifest_path or "")
            portable_payload = portable_manifest_payload(updated_manifest, run_dir)
            if read_json(manifest_snapshot) != portable_payload:
                write_json_atomic(manifest_snapshot, portable_payload)
                state.manifest_digest = _digest_payload(portable_payload)
                manifest = updated_manifest
            else:
                manifest = updated_manifest

            specs, blocker = build_execution_specs(
                request,
                manifest,
                dataset_paths,
            )
            if blocker is not None:
                _finish_blocked(
                    state,
                    request,
                    run_dir,
                    blocker,
                    actor=AgentName.IMPLEMENTATION,
                )
                return _implementation_response(state, run_dir)
            specs_path = safe_join(run_dir, EXECUTION_SPECS_PATH)
            write_json_atomic(
                specs_path,
                [item.model_dump(mode="json") for item in specs],
            )
            state.execution_specs_path = EXECUTION_SPECS_PATH
            state.execution_specs_digest = _digest_file(specs_path)
            review_path = safe_join(run_dir, IMPLEMENTATION_REVIEW_PATH)
            write_json_atomic(review_path, review.model_dump(mode="json"))
            state.implementation_review_path = IMPLEMENTATION_REVIEW_PATH
            state.implementation_review_digest = _digest_file(review_path)
            code_context = build_code_review_context(
                request,
                manifest,
                run_dir,
                dataset_paths,
                [risk for item in review.items for risk in item.risks],
            )
            code_context_path = safe_join(run_dir, CODE_REVIEW_CONTEXT_PATH)
            write_json_atomic(
                code_context_path,
                code_context.model_dump(mode="json"),
            )
            state.code_review_context_path = CODE_REVIEW_CONTEXT_PATH
            state.code_review_context_digest = _digest_file(code_context_path)
            if state.current_stage == AgentRunStage.DATA_READY.value:
                _transition(
                    state,
                    AgentRunStage.CODE_REVIEW_REQUIRED,
                    actor=AgentName.IMPLEMENTATION,
                    summary=(
                        f"Implementation Builder已生成{len(specs)}个任务；"
                        "等待根experiment-agent在隔离上下文中执行第一阶段代码审查"
                    ),
                    artifact_paths=[
                        IMPLEMENTATION_REVIEW_PATH,
                        CODE_REVIEW_CONTEXT_PATH,
                        EXECUTION_SPECS_PATH,
                    ],
                )
            _save_state(state, run_dir)
            return _implementation_response(state, run_dir)

    def write_generated_proposal(
        self,
        run_dir: Path,
        *,
        run_id: str,
        proposal: GeneratedImplementationProposal | dict[str, Any],
    ) -> ImplementationAgentResponse:
        """Persist generated source through a run-scoped, non-executing writer."""

        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        validated = (
            proposal
            if isinstance(proposal, GeneratedImplementationProposal)
            else GeneratedImplementationProposal.model_validate(proposal)
        )
        # Resolve curated families before taking the writer lock. Calling the
        # standard resolver while already holding this non-reentrant lock would
        # deadlock the same process and leave a misleading "concurrent" error.
        if _standard_family(validated.method) is not None:
            return self.resolve_implementation(run_dir, run_id=run_id)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir, expected_run_id=run_id
            )
            if state.current_stage != AgentRunStage.DATA_READY.value:
                raise ValueError("generated proposals may be written only from DATA_READY")
            if (
                state.implementation_inspection_path is None
                or state.implementation_inspection_digest is None
            ):
                raise ValueError(
                    "inspect must run before writing a generated implementation"
                )
            updated = write_generated_implementation(
                validated,
                request,
                manifest,
                run_dir,
            )
            manifest_path = safe_join(run_dir, state.manifest_path or "")
            payload = portable_manifest_payload(updated, run_dir)
            write_json_atomic(manifest_path, payload)
            state.manifest_digest = _digest_payload(payload)
            state.warnings = _deduplicate(
                [
                    *state.warnings,
                    f"方法{validated.method}的生成代码已受限写入，尚未冒烟测试或批准",
                ]
            )
            _save_state(state, run_dir)
            return _implementation_response(state, run_dir)

    def review_context(self, run_dir: Path, *, run_id: str) -> RootReviewResponse:
        """Return only isolated, verifiable material to the root reviewer."""

        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir,
                expected_run_id=run_id,
                accept_manifest_drift=True,
            )
            if state.current_stage == AgentRunStage.EXECUTION_REVIEW_REQUIRED.value:
                if implementation_code_digest(manifest, run_dir) != state.reviewed_code_digest:
                    _invalidate_reviews_locked(
                        state,
                        request,
                        manifest,
                        run_dir,
                        "代码、命令、依赖或实现清单在冒烟后发生变化",
                    )
            if state.current_stage == AgentRunStage.CODE_REVIEW_REQUIRED.value:
                _refresh_code_review_context(state, request, manifest, run_dir)
            _save_state(state, run_dir)
            return _root_review_response(state, run_dir)

    def submit_code_review(
        self,
        run_dir: Path,
        *,
        run_id: str,
        decision: CodeReviewDecision | dict[str, Any],
    ) -> RootReviewResponse:
        """Validate root review, then run a bounded, non-scientific smoke test."""

        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir,
                expected_run_id=run_id,
                accept_manifest_drift=True,
            )
            if state.current_stage != AgentRunStage.CODE_REVIEW_REQUIRED.value:
                raise ValueError("code review requires CODE_REVIEW_REQUIRED")
            reviewed = (
                decision
                if isinstance(decision, CodeReviewDecision)
                else CodeReviewDecision.model_validate(decision)
            )
            context = _load_code_review_context(state, run_dir)
            current_code_digest = implementation_code_digest(manifest, run_dir)
            if (
                reviewed.code_digest != context.code_digest
                or reviewed.code_digest != current_code_digest
            ):
                _refresh_code_review_context(state, request, manifest, run_dir)
                _save_state(state, run_dir)
                raise ValueError("code digest does not match the isolated review context")
            decision_path = safe_join(run_dir, CODE_REVIEW_PATH)
            write_json_atomic(decision_path, reviewed.model_dump(mode="json"))
            state.code_review_path = CODE_REVIEW_PATH
            state.code_review_digest = _digest_file(decision_path)
            state.reviewed_code_digest = reviewed.code_digest
            if reviewed.decision == "REPLAN":
                _finish_blocked(
                    state,
                    request,
                    run_dir,
                    StageBlocker(
                        reason=reviewed.reason,
                        affected_experiment_ids=request.experiment_plan.primary_experiments,
                        blockers=[
                            *reviewed.security_findings,
                            *reviewed.scientific_findings,
                            *reviewed.dependency_findings,
                        ] or ["根Agent判定规划信息不足"],
                        suggested_changes=reviewed.required_changes or ["补充明确规划后重试"],
                    ),
                    actor=AgentName.ROOT,
                )
                return _root_review_response(state, run_dir)
            if reviewed.decision == "REJECT":
                _transition(
                    state,
                    AgentRunStage.DATA_READY,
                    actor=AgentName.ROOT,
                    summary="第一阶段独立代码审查拒绝，退回Implementation Agent修改",
                    artifact_paths=[CODE_REVIEW_PATH],
                )
                _save_state(state, run_dir)
                return _root_review_response(state, run_dir)
            if not context.static_report.passed:
                raise ValueError("deterministic static gates failed; LLM cannot authorize smoke")
            if request.execution_config.dry_run:
                raise ValueError("dry_run=true cannot authorize implementation smoke tests")

            try:
                deployment = prepare_execution_environment(
                    manifest,
                    run_dir,
                    mode=request.execution_config.environment_mode,
                    allow_dependency_install=(
                        request.execution_config.allow_dependency_install
                    ),
                )
            except EnvironmentDeploymentError as exc:
                _finish_blocked(
                    state,
                    request,
                    run_dir,
                    StageBlocker(
                        reason="实验环境部署或依赖校验失败",
                        affected_experiment_ids=(
                            request.experiment_plan.primary_experiments
                        ),
                        blockers=[f"[compute] {exc}"],
                        suggested_changes=[
                            "修正并固定实现依赖；确需联网安装时，"
                            "在模块三输入中显式设置allow_dependency_install=true"
                        ],
                    ),
                    actor=AgentName.ROOT,
                )
                return _root_review_response(state, run_dir)
            state.environment_deployment_path = deployment.report_path
            state.environment_deployment_digest = _digest_file(
                safe_join(run_dir, deployment.report_path)
            )

            specs = _load_execution_specs(state, run_dir)
            _verify_dataset_fingerprints(state, run_dir)
            planned = sorted({item.method for item in specs})
            results: list[RuntimeRunResult] = []
            payload = manifest.model_dump(mode="python")
            _clear_manifest_approval(payload)
            for method in planned:
                definition = manifest.implementations[method]
                source_spec = next(item for item in specs if item.method == method)
                smoke_spec = source_spec.model_copy(
                    update={"run_record_id": f"smoke-{source_spec.run_record_id}"}
                )
                try:
                    bounded_dataset = _bounded_smoke_dataset(
                        Path(source_spec.dataset_path),
                        run_dir,
                        dataset_name=source_spec.dataset,
                    )
                    smoke_spec = smoke_spec.model_copy(
                        update={"dataset_path": str(bounded_dataset)}
                    )
                    result = execute_one(
                        smoke_spec,
                        definition,
                        run_dir,
                        max_retries=0,
                        timeout_seconds=min(
                            _timeout_seconds(request), _SMOKE_TIMEOUT_SECONDS
                        ),
                        metric_definitions=manifest.metrics,
                        python_executable=deployment.python_executable,
                    )
                except Exception as exc:
                    result = RuntimeRunResult(
                        run_record_id=smoke_spec.run_record_id,
                        experiment_id=smoke_spec.experiment_id,
                        method=smoke_spec.method,
                        success=False,
                        attempts=1,
                        duration_seconds=0,
                        uses_gpu=definition.uses_gpu,
                        command="blocked before smoke execution",
                        config_path=f"configs/{smoke_spec.run_record_id}.json",
                        log_path=f"logs/{smoke_spec.run_record_id}.log",
                        metrics=[],
                        error=f"smoke gate failed: {exc}",
                    )
                try:
                    result = _attach_smoke_evidence(result, run_dir)
                except (OSError, ValueError) as exc:
                    result = result.model_copy(
                        update={
                            "success": False,
                            "metrics": [],
                            "evidence_files": [],
                            "error": f"smoke evidence capture failed: {exc}",
                        }
                    )
                results.append(result)
                item = dict(payload["implementations"][method])
                item["ready"] = result.success
                item["smoke_test_passed"] = result.success
                item["smoke_test_command"] = list(definition.command)
                payload["implementations"][method] = item
            try:
                _verify_dataset_fingerprints(state, run_dir)
            except (OSError, ValueError) as exc:
                raise ValueError(f"smoke test modified prepared data: {exc}") from exc
            smoke_path = safe_join(run_dir, SMOKE_RESULTS_PATH)
            write_json_atomic(
                smoke_path,
                [item.model_dump(mode="json") for item in results],
            )
            state.smoke_results_path = SMOKE_RESULTS_PATH
            state.smoke_results_digest = _digest_file(smoke_path)
            updated = ImplementationManifest.model_validate(payload)
            manifest_path = safe_join(run_dir, state.manifest_path or "")
            portable_payload = portable_manifest_payload(updated, run_dir)
            write_json_atomic(manifest_path, portable_payload)
            state.manifest_digest = _digest_payload(portable_payload)
            if implementation_code_digest(updated, run_dir) != reviewed.code_digest:
                raise ValueError("implementation code digest changed during smoke execution")
            execution_context = _build_execution_review_context(
                state,
                request,
                updated,
                run_dir,
                results,
                context.static_report,
            )
            execution_context_path = safe_join(
                run_dir, EXECUTION_REVIEW_CONTEXT_PATH
            )
            write_json_atomic(
                execution_context_path,
                execution_context.model_dump(mode="json"),
            )
            state.execution_review_context_path = EXECUTION_REVIEW_CONTEXT_PATH
            state.execution_review_context_digest = _digest_file(
                execution_context_path
            )
            _transition(
                state,
                AgentRunStage.EXECUTION_REVIEW_REQUIRED,
                actor=AgentName.ROOT,
                summary=(
                    "第一阶段独立审查已授权并完成确定性冒烟；"
                    "等待根Agent第二阶段执行审查"
                ),
                artifact_paths=[
                    CODE_REVIEW_PATH,
                    SMOKE_RESULTS_PATH,
                    EXECUTION_REVIEW_CONTEXT_PATH,
                    DEPLOYMENT_PATH,
                ],
            )
            _save_state(state, run_dir)
            return _root_review_response(state, run_dir)

    def submit_execution_review(
        self,
        run_dir: Path,
        *,
        run_id: str,
        decision: ExecutionReviewDecision | dict[str, Any],
    ) -> RootReviewResponse:
        """Bind a second independent review after all deterministic gates pass."""

        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir,
                expected_run_id=run_id,
                accept_manifest_drift=True,
            )
            if state.current_stage != AgentRunStage.EXECUTION_REVIEW_REQUIRED.value:
                raise ValueError("execution review requires EXECUTION_REVIEW_REQUIRED")
            reviewed = (
                decision
                if isinstance(decision, ExecutionReviewDecision)
                else ExecutionReviewDecision.model_validate(decision)
            )
            context = _load_execution_review_context(state, run_dir)
            current_code_digest = implementation_code_digest(manifest, run_dir)
            if (
                reviewed.code_digest != state.reviewed_code_digest
                or reviewed.code_digest != context.code_digest
                or reviewed.code_digest != current_code_digest
            ):
                _invalidate_reviews_locked(
                    state,
                    request,
                    manifest,
                    run_dir,
                    "第二阶段审查前代码摘要发生变化",
                )
                _save_state(state, run_dir)
                raise ValueError("code digest changed after first-stage review")
            if (
                reviewed.smoke_result_digest != state.smoke_results_digest
                or reviewed.smoke_result_digest != context.smoke_result_digest
                or _digest_file(safe_join(run_dir, state.smoke_results_path or ""))
                != reviewed.smoke_result_digest
            ):
                _invalidate_reviews_locked(
                    state,
                    request,
                    manifest,
                    run_dir,
                    "冒烟结果摘要发生变化",
                )
                _save_state(state, run_dir)
                raise ValueError("smoke result digest changed before execution review")
            if (
                state.environment_deployment_path is None
                or state.environment_deployment_digest is None
                or context.environment_deployment_digest
                != state.environment_deployment_digest
                or _digest_file(
                    safe_join(run_dir, state.environment_deployment_path)
                )
                != state.environment_deployment_digest
            ):
                _invalidate_reviews_locked(
                    state,
                    request,
                    manifest,
                    run_dir,
                    "实验环境部署证据发生变化",
                )
                _save_state(state, run_dir)
                raise ValueError(
                    "environment deployment changed before execution review"
                )
            code_review = CodeReviewDecision.model_validate(
                read_json(safe_join(run_dir, state.code_review_path or ""))
            )
            if reviewed.review_model != code_review.review_model:
                raise ValueError("both reviews must identify the same root reviewer model")
            decision_path = safe_join(run_dir, EXECUTION_REVIEW_PATH)
            write_json_atomic(decision_path, reviewed.model_dump(mode="json"))
            state.execution_review_path = EXECUTION_REVIEW_PATH
            state.execution_review_digest = _digest_file(decision_path)
            if reviewed.decision == "REPLAN":
                _finish_blocked(
                    state,
                    request,
                    run_dir,
                    StageBlocker(
                        reason=reviewed.reason,
                        affected_experiment_ids=request.experiment_plan.primary_experiments,
                        blockers=reviewed.risks or ["第二阶段审查发现科学契约不明确"],
                        suggested_changes=["按执行审查结论补充模块二规划或实现契约"],
                    ),
                    actor=AgentName.ROOT,
                )
                return _root_review_response(state, run_dir)
            if reviewed.decision == "REJECT":
                _transition(
                    state,
                    AgentRunStage.DATA_READY,
                    actor=AgentName.ROOT,
                    summary="第二阶段独立执行审查拒绝，退回Implementation Agent修改",
                    artifact_paths=[EXECUTION_REVIEW_PATH, SMOKE_RESULTS_PATH],
                )
                _save_state(state, run_dir)
                return _root_review_response(state, run_dir)

            smoke_results = _load_smoke_results(state, run_dir)
            if not smoke_results or not all(item.success for item in smoke_results):
                raise ValueError("formal approval requires every bounded smoke test to pass")
            planned = {item.method for item in _load_execution_specs(state, run_dir)}
            if not all(manifest.implementations[name].ready for name in planned):
                raise ValueError("formal approval requires ready=true for every planned method")
            if not all(
                manifest.metrics[name].verified
                for name in request.experiment_plan.metrics
            ):
                raise ValueError("formal approval requires verified=true for every metric")
            static_report = deterministic_static_review(request, manifest, run_dir)
            if not static_report.passed:
                raise ValueError("deterministic static gates no longer pass")
            if request.execution_config.dry_run:
                raise ValueError("dry_run=true cannot be approved for scientific execution")

            approval_digest = implementation_approval_digest(manifest, run_dir)
            payload = manifest.model_dump(mode="python")
            payload.update(
                {
                    "execution_approved": True,
                    "approval_digest": approval_digest,
                    "approved_by": "experiment-agent",
                    "approved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "approval_type": "LLM_REVIEW",
                    "reviewer_model": reviewed.review_model,
                    "review_digest": review_decision_digest(reviewed),
                    "reviewed_code_digest": reviewed.code_digest,
                    "reviewed_smoke_digest": reviewed.smoke_result_digest,
                }
            )
            approved = ImplementationManifest.model_validate(payload)
            manifest_path = safe_join(run_dir, state.manifest_path or "")
            portable_payload = portable_manifest_payload(approved, run_dir)
            write_json_atomic(manifest_path, portable_payload)
            state.manifest_digest = _digest_payload(portable_payload)
            state.implementation_approval_digest = approval_digest
            _transition(
                state,
                AgentRunStage.IMPLEMENTATION_READY,
                actor=AgentName.ROOT,
                summary=(
                    "根experiment-agent完成两阶段独立审查；"
                    "确定性后端已绑定代码、冒烟和审查摘要"
                ),
                artifact_paths=[
                    CODE_REVIEW_PATH,
                    SMOKE_RESULTS_PATH,
                    EXECUTION_REVIEW_PATH,
                    state.manifest_path or "inputs/implementation-manifest.json",
                ],
            )
            _save_state(state, run_dir)
            return _root_review_response(state, run_dir)

    def execute(self, run_dir: Path, *, run_id: str) -> ExecutionAgentResponse:
        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(
                run_dir,
                expected_run_id=run_id,
                accept_manifest_drift=True,
            )
            if state.current_stage in _TERMINAL_STAGES:
                return _execution_response(state, run_dir)
            if state.current_stage != AgentRunStage.IMPLEMENTATION_READY.value:
                if _stage_rank(state.current_stage) > _stage_rank(
                    AgentRunStage.IMPLEMENTATION_READY.value
                ):
                    return _execution_response(state, run_dir)
                raise ValueError(
                    "ExecutionAgent requires IMPLEMENTATION_READY; current stage is "
                    f"{state.current_stage}"
                )
            try:
                _verify_execution_approval(state, manifest, run_dir)
            except ValueError as exc:
                _invalidate_reviews_locked(
                    state,
                    request,
                    manifest,
                    run_dir,
                    f"正式执行前摘要校验失败: {exc}",
                )
                _save_state(state, run_dir)
                return _execution_response(state, run_dir)
            specs = _load_execution_specs(state, run_dir)
            _verify_dataset_fingerprints(state, run_dir)
            if (
                state.environment_deployment_path is None
                or state.environment_deployment_digest is None
            ):
                raise ValueError("formal execution requires a deployed environment")
            deployment_path = safe_join(
                run_dir, state.environment_deployment_path
            )
            if _digest_file(deployment_path) != state.environment_deployment_digest:
                raise ValueError("environment deployment report changed after review")
            python_executable = load_execution_python(run_dir)
            validation_lock = threading.Lock()

            def validate_during_execution() -> None:
                with validation_lock:
                    _verify_dataset_fingerprints(state, run_dir)

            runtime_results = execute_all(
                specs,
                manifest,
                run_dir,
                max_retries=request.execution_config.max_retries,
                timeout_seconds=_timeout_seconds(request),
                max_parallel_runs=request.execution_config.max_parallel_runs,
                max_parallel_gpu_runs=(
                    request.execution_config.max_parallel_gpu_runs
                ),
                monitor_interval_seconds=(
                    request.execution_config.monitor_interval_seconds
                ),
                live_validator=validate_during_execution,
                python_executable=python_executable,
            )
            write_runtime_results(runtime_results, run_dir)
            state.runtime_results_path = RUNTIME_RESULTS_PATH
            state.runtime_results_digest = _digest_file(
                safe_join(run_dir, RUNTIME_RESULTS_PATH)
            )
            try:
                _verify_dataset_fingerprints(state, run_dir)
            except (OSError, ValueError) as exc:
                _finish_failed(
                    state,
                    request,
                    run_dir,
                    runtime_results,
                    errors=[f"实验运行期间数据集发生变化: {exc}"],
                    actor=AgentName.EXECUTION,
                )
                return _execution_response(state, run_dir)
            successful = [item for item in runtime_results if item.success]
            failed = [item for item in runtime_results if not item.success]
            specs_by_run_id = {item.run_record_id: item for item in specs}
            successful_primary = [
                item
                for item in successful
                if specs_by_run_id[item.run_record_id].experiment_type == "PRIMARY"
            ]
            if not successful_primary:
                failed_primary = sorted(
                    {
                        (
                            specs_by_run_id[item.run_record_id].experiment_id,
                            specs_by_run_id[item.run_record_id].method,
                        )
                        for item in runtime_results
                        if specs_by_run_id[item.run_record_id].experiment_type
                        == "PRIMARY"
                    }
                )
                _finish_failed(
                    state,
                    request,
                    run_dir,
                    runtime_results,
                    errors=[
                        "没有任何核心主方法运行成功；失败核心任务="
                        f"{failed_primary}；详见{RUNTIME_RESULTS_PATH}"
                    ],
                    actor=AgentName.EXECUTION,
                )
                return _execution_response(state, run_dir)
            if failed:
                state.warnings = _deduplicate(
                    [
                        *state.warnings,
                        f"{len(failed)}个运行失败或耗尽重试，"
                        f"结果仅基于{len(successful)}个成功运行",
                    ]
                )
            _transition(
                state,
                AgentRunStage.EXECUTION_READY,
                actor=AgentName.EXECUTION,
                summary=(
                    f"实验执行完成：{len(successful)}成功，{len(failed)}失败"
                ),
                artifact_paths=[RUNTIME_RESULTS_PATH, MONITOR_PATH, EVENTS_PATH],
            )
            _save_state(state, run_dir)
            return _execution_response(state, run_dir)

    def analyze(self, run_dir: Path, *, run_id: str) -> AnalysisAgentResponse:
        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        with _stage_lock(run_dir):
            state, request, manifest = _load_context(run_dir, expected_run_id=run_id)
            if state.current_stage in _TERMINAL_STAGES:
                return _analysis_response(state, run_dir)
            if state.current_stage != AgentRunStage.EXECUTION_READY.value:
                raise ValueError(
                    "AnalysisAgent requires EXECUTION_READY; current stage is "
                    f"{state.current_stage}"
                )
            specs = _load_execution_specs(state, run_dir)
            runtime_results = _load_runtime_results(state, run_dir)
            successful = [item for item in runtime_results if item.success]
            failed = [item for item in runtime_results if not item.success]
            if not successful:
                _finish_failed(
                    state,
                    request,
                    run_dir,
                    runtime_results,
                    errors=["没有成功运行可供分析"],
                    actor=AgentName.ANALYSIS,
                )
                return _analysis_response(state, run_dir)

            experiment_runs, metric_records = materialize_execution_records(
                specs,
                runtime_results,
                manifest,
            )
            aggregates = aggregate_metrics(metric_records, manifest)
            if not aggregates:
                _finish_failed(
                    state,
                    request,
                    run_dir,
                    runtime_results,
                    errors=["没有形成任何成功test指标，无法生成科研结果"],
                    actor=AgentName.ANALYSIS,
                )
                return _analysis_response(state, run_dir)

            try:
                head_statistics = build_head_statistics(aggregates, request)
            except ValueError as exc:
                blocker = StageBlocker(
                    reason="主结果统计映射存在歧义",
                    affected_experiment_ids=(
                        request.experiment_plan.primary_experiments
                    ),
                    blockers=[str(exc)],
                    suggested_changes=[
                        "请模块二为每个主指标明确唯一主方法和主参数配置"
                    ],
                )
                _finish_blocked(
                    state,
                    request,
                    run_dir,
                    blocker,
                    actor=AgentName.ANALYSIS,
                )
                return _analysis_response(state, run_dir)

            analyses = build_analysis_records(aggregates, request)
            key_findings, finding_records = build_findings(aggregates, request)
            criterion_evaluations = evaluate_criteria(aggregates, request)
            hypothesis_evaluations = evaluate_hypotheses(
                criterion_evaluations,
                request,
            )
            (
                visualization_data,
                visualization_candidates,
                tables,
                figures,
                table_artifacts,
                figure_artifacts,
                writing_brief_path,
            ) = build_visualization_package(
                metric_records,
                aggregates,
                analyses,
                finding_records,
                run_dir,
                domain_name=request.domain.domain_name,
                primary_dataset=request.experiment_plan.datasets[0].name,
                baseline_names={
                    item.name for item in request.experiment_plan.baselines
                },
                minimum_figure_candidates=(
                    request.execution_config.min_figure_candidates
                ),
                maximum_figure_candidates=(
                    request.execution_config.max_figure_candidates
                ),
            )
            context_path = write_analysis_context(
                run_dir,
                request,
                metric_records,
                aggregates,
                finding_records,
                analyses,
                visualization_data,
                visualization_candidates,
            )
            extension_output, extension_warnings, extension_failures = (
                run_analysis_extensions(
                    manifest,
                    run_dir,
                    context_path,
                    timeout_seconds=_timeout_seconds(request),
                    reserved_table_names=set(tables),
                    reserved_figure_names=set(figures),
                )
            )
            state.warnings = _deduplicate(
                [
                    *state.warnings,
                    *extension_warnings,
                    *extension_failures,
                ]
            )
            analyses.extend(extension_output.analysis_records)
            visualization_data.extend(extension_output.visualization_data)
            visualization_candidates.extend(
                extension_output.visualization_candidates
            )
            tables.update(extension_output.tables)
            figures.update(extension_output.figures)
            table_artifacts.extend(extension_output.table_artifacts)
            figure_artifacts.extend(extension_output.figure_artifacts)
            figure_candidate_count = sum(
                item.kind == "FIGURE" for item in visualization_candidates
            )
            effective_figure_target = min(
                request.execution_config.min_figure_candidates,
                request.execution_config.max_figure_candidates,
                8,
            )
            figure_shortfall = (
                figure_candidate_count
                < effective_figure_target
            )
            if figure_shortfall:
                state.warnings = _deduplicate(
                    [
                        *state.warnings,
                        "真实数据维度不足，只能形成"
                        f"{figure_candidate_count}幅非重复候选图，少于配置的"
                        f"{effective_figure_target}幅独立证据图目标；"
                        "模块三拒绝用重复换皮图凑数",
                    ]
                )
            environment_path, environment_warnings = write_environment(
                run_dir, manifest
            )
            state.warnings = _deduplicate(
                [*state.warnings, *environment_warnings]
            )

            experiment_results = ExperimentResults(
                key_findings=key_findings,
                tables=tables,
                figures=figures,
                statistics=head_statistics,
                finding_records=finding_records,
                metric_records=metric_records,
                experiment_runs=experiment_runs,
                hypothesis_evaluations=hypothesis_evaluations,
                metric_implementations=_metric_implementations(request, manifest),
                baseline_implementations=_baseline_implementations(
                    request, manifest, run_dir
                ),
                criterion_evaluations=criterion_evaluations,
                analysis_records=analyses,
                visualization_data=visualization_data,
                visualization_candidates=visualization_candidates,
                writing_brief_path=writing_brief_path,
                table_artifacts=table_artifacts,
                figure_artifacts=figure_artifacts,
            )
            output = ExperimentModuleOutput(
                schema_version=request.schema_version,
                run_id=request.run_id,
                status=(
                    "PARTIAL"
                    if failed or extension_failures or figure_shortfall
                    else "PASS"
                ),
                experiment_results=experiment_results,
                resource_usage=_resource_usage(
                    state.started_at_epoch,
                    runtime_results,
                ),
                reproducibility=Reproducibility(
                    environment_path=relative_to_run(environment_path, run_dir),
                    environment_deployment_path=DEPLOYMENT_PATH,
                    entry_command=reproducibility_command(),
                    seeds=request.execution_config.seeds,
                    results_csv_path="visualization/raw_metrics.csv",
                    raw_results_dir="raw_results",
                    logs_dir="logs",
                    execution_monitor_path=MONITOR_PATH,
                    execution_events_path=EVENTS_PATH,
                ),
                planning_feedback=None,
                warnings=state.warnings,
                errors=[],
            )
            write_module_output(output, run_dir)
            state.output_path = MODULE_OUTPUT_PATH
            state.output_digest = _digest_file(
                safe_join(run_dir, MODULE_OUTPUT_PATH)
            )
            try:
                artifact_manifest = _write_artifact_manifest(
                    output,
                    state,
                    run_dir,
                )
            except (OSError, ValueError) as exc:
                _finish_failed(
                    state,
                    request,
                    run_dir,
                    runtime_results,
                    errors=[f"模块四交付清单生成失败: {exc}"],
                    actor=AgentName.ANALYSIS,
                )
                return _analysis_response(state, run_dir)
            state.artifact_manifest_path = ARTIFACT_MANIFEST_PATH
            state.artifact_manifest_digest = _digest_file(artifact_manifest)
            _transition(
                state,
                AgentRunStage.COMPLETED,
                actor=AgentName.ANALYSIS,
                summary=(
                    f"分析与可视化完成，模块状态为{output.status}，"
                    f"生成{len(visualization_candidates)}个候选图表"
                ),
                artifact_paths=[
                    MODULE_OUTPUT_PATH,
                    ARTIFACT_MANIFEST_PATH,
                    "visualization/raw_metrics.csv",
                    "visualization/aggregated_metrics.csv",
                ],
            )
            _save_state(state, run_dir)
            return _analysis_response(state, run_dir)

    def status(self, run_dir: Path, *, run_id: str) -> InitializeResponse:
        run_dir = _resolve_resume_run_dir(run_dir, base_dir=self.base_dir)
        state = _load_state(run_dir)
        if state.run_id != run_id:
            raise ValueError(
                f"state run_id mismatch: expected {run_id}, got {state.run_id}"
            )
        return _initialize_response(state, run_dir)

    def run_all(
        self,
        payload: ExperimentModuleInput | dict[str, Any],
    ) -> ExperimentModuleOutput:
        request = validate_request(payload)
        run_dir = resolve_run_dir(
            request.execution_config.run_dir,
            base_dir=self.base_dir,
        )
        initialized = self.initialize(request)
        if initialized.terminal:
            return _read_output(run_dir)
        data = self.prepare_data(run_dir, run_id=request.run_id)
        if data.terminal:
            return _read_output(run_dir)
        implementation = self.resolve_implementation(
            run_dir,
            run_id=request.run_id,
        )
        if implementation.terminal:
            return _read_output(run_dir)
        if implementation.approval_required:
            state, saved_request, _manifest = _load_context(
                run_dir, expected_run_id=request.run_id
            )
            blocker = StageBlocker(
                reason="Implementation Builder需要独立LLM审查",
                affected_experiment_ids=request.experiment_plan.primary_experiments,
                blockers=[
                    f"代码审查上下文待处理: {state.code_review_context_path}"
                ],
                suggested_changes=[
                    "使用experiment-agent的隔离审查上下文完成两阶段审查；"
                    "无LLM CLI不会自行生成审查结论或执行"
                ],
            )
            _finish_blocked(
                state,
                saved_request,
                run_dir,
                blocker,
                actor=AgentName.ROOT,
            )
            return _read_output(run_dir)
        execution = self.execute(run_dir, run_id=request.run_id)
        if execution.terminal:
            return _read_output(run_dir)
        analysis = self.analyze(run_dir, run_id=request.run_id)
        if not analysis.terminal:
            raise RuntimeError("analysis stage did not produce a terminal output")
        return _read_output(run_dir)


def _clear_manifest_approval(payload: dict[str, Any]) -> None:
    payload["execution_approved"] = False
    for field in (
        "approval_digest",
        "approved_by",
        "approved_at_utc",
        "approval_type",
        "reviewer_model",
        "review_digest",
        "reviewed_code_digest",
        "reviewed_smoke_digest",
    ):
        payload[field] = None


def _refresh_code_review_context(
    state: ExperimentAgentState,
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
) -> CodeReviewContext:
    risks: list[str] = []
    if state.implementation_review_path and state.implementation_review_digest:
        path = safe_join(run_dir, state.implementation_review_path)
        if _digest_file(path) != state.implementation_review_digest:
            raise ValueError("deterministic implementation review digest changed")
        review = ImplementationReview.model_validate(read_json(path))
        risks = [risk for item in review.items for risk in item.risks]
    dataset_paths = {
        name: str(safe_join(run_dir, path)) for name, path in state.dataset_paths.items()
    }
    context = build_code_review_context(
        request,
        manifest,
        run_dir,
        dataset_paths,
        risks,
    )
    path = safe_join(run_dir, CODE_REVIEW_CONTEXT_PATH)
    write_json_atomic(path, context.model_dump(mode="json"))
    state.code_review_context_path = CODE_REVIEW_CONTEXT_PATH
    state.code_review_context_digest = _digest_file(path)
    return context


def _load_code_review_context(
    state: ExperimentAgentState,
    run_dir: Path,
) -> CodeReviewContext:
    if not state.code_review_context_path or not state.code_review_context_digest:
        raise ValueError("isolated code review context is missing")
    path = safe_join(run_dir, state.code_review_context_path)
    if _digest_file(path) != state.code_review_context_digest:
        raise ValueError("isolated code review context digest changed")
    return CodeReviewContext.model_validate(read_json(path))


def _load_execution_review_context(
    state: ExperimentAgentState,
    run_dir: Path,
) -> ExecutionReviewContext:
    if (
        not state.execution_review_context_path
        or not state.execution_review_context_digest
    ):
        raise ValueError("isolated execution review context is missing")
    path = safe_join(run_dir, state.execution_review_context_path)
    if _digest_file(path) != state.execution_review_context_digest:
        raise ValueError("isolated execution review context digest changed")
    return ExecutionReviewContext.model_validate(read_json(path))


def _load_smoke_results(
    state: ExperimentAgentState,
    run_dir: Path,
) -> list[RuntimeRunResult]:
    if not state.smoke_results_path or not state.smoke_results_digest:
        raise ValueError("smoke results are missing")
    path = safe_join(run_dir, state.smoke_results_path)
    if _digest_file(path) != state.smoke_results_digest:
        raise ValueError("smoke result digest changed")
    payload = read_json(path)
    if not isinstance(payload, list):
        raise ValueError("smoke results must contain a list")
    results = [RuntimeRunResult.model_validate(item) for item in payload]
    for result in results:
        _verify_smoke_evidence(result, run_dir)
    return results


def _build_execution_review_context(
    state: ExperimentAgentState,
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
    results: list[RuntimeRunResult],
    static_report: Any,
) -> ExecutionReviewContext:
    if not state.code_review_path or not state.code_review_digest:
        raise ValueError("first-stage code review evidence is missing")
    if not state.smoke_results_digest:
        raise ValueError("smoke result digest is missing")
    if (
        not state.environment_deployment_path
        or not state.environment_deployment_digest
    ):
        raise ValueError("environment deployment evidence is missing")
    deployment_path = safe_join(run_dir, state.environment_deployment_path)
    if _digest_file(deployment_path) != state.environment_deployment_digest:
        raise ValueError("environment deployment evidence changed")
    planned = sorted({item.method for item in _load_execution_specs(state, run_dir)})
    return ExecutionReviewContext(
        run_id=request.run_id,
        code_digest=implementation_code_digest(manifest, run_dir),
        code_material=_load_code_review_context(state, run_dir),
        code_review_path=state.code_review_path,
        code_review_digest=state.code_review_digest,
        smoke_result_digest=state.smoke_results_digest,
        smoke_results=results,
        environment_deployment=read_json(deployment_path),
        environment_deployment_digest=state.environment_deployment_digest,
        metric_contracts={
            name: manifest.metrics[name].model_dump(mode="json")
            for name in request.experiment_plan.metrics
        },
        commands={
            method: portable_arguments(
                manifest.implementations[method].command,
                run_dir=run_dir,
            )
            for method in planned
        },
        data_access={
            method: list(state.dataset_paths.values()) for method in planned
        },
        smoke_data_access=_smoke_data_access(results, run_dir),
        static_report=static_report,
        risks=[
            f"{item.method}: {item.error}"
            for item in results
            if not item.success and item.error
        ],
    )


def _invalidate_reviews_locked(
    state: ExperimentAgentState,
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
    reason: str,
) -> None:
    payload = manifest.model_dump(mode="python")
    _clear_manifest_approval(payload)
    planned = {item.method for item in _load_execution_specs(state, run_dir)}
    for method in planned:
        if method not in payload["implementations"]:
            continue
        item = dict(payload["implementations"][method])
        item["ready"] = False
        item["smoke_test_passed"] = False
        payload["implementations"][method] = item
    invalidated = ImplementationManifest.model_validate(payload)
    manifest_path = safe_join(run_dir, state.manifest_path or "")
    portable_payload = portable_manifest_payload(invalidated, run_dir)
    write_json_atomic(manifest_path, portable_payload)
    state.manifest_digest = _digest_payload(portable_payload)
    state.implementation_approval_digest = None
    state.code_review_path = None
    state.code_review_digest = None
    state.reviewed_code_digest = None
    state.smoke_results_path = None
    state.smoke_results_digest = None
    state.execution_review_context_path = None
    state.execution_review_context_digest = None
    state.execution_review_path = None
    state.execution_review_digest = None
    state.environment_deployment_path = None
    state.environment_deployment_digest = None
    _refresh_code_review_context(state, request, invalidated, run_dir)
    _transition(
        state,
        AgentRunStage.CODE_REVIEW_REQUIRED,
        actor=AgentName.ROOT,
        summary=f"两阶段审查已自动失效: {reason}",
        artifact_paths=[
            CODE_REVIEW_CONTEXT_PATH,
            state.manifest_path or "inputs/implementation-manifest.json",
        ],
    )


def _root_review_response(
    state: ExperimentAgentState,
    run_dir: Path,
) -> RootReviewResponse:
    context_path = None
    context_payload = None
    review_phase = None
    if state.current_stage == AgentRunStage.CODE_REVIEW_REQUIRED.value:
        context = _load_code_review_context(state, run_dir)
        context_path = state.code_review_context_path
        context_payload = context.model_dump(mode="json")
        review_phase = state.current_stage
    elif state.current_stage == AgentRunStage.EXECUTION_REVIEW_REQUIRED.value:
        context = _load_execution_review_context(state, run_dir)
        context_path = state.execution_review_context_path
        context_payload = context.model_dump(mode="json")
        review_phase = state.current_stage
    planning_feedback, errors = _terminal_details(state, run_dir)
    return RootReviewResponse(
        run_id=state.run_id,
        run_dir=str(run_dir),
        consumer=_consumer_for_stage(state.current_stage),
        stage=state.current_stage,
        terminal=state.current_stage in _TERMINAL_STAGES,
        review_phase=review_phase,
        review_context_path=context_path,
        review_context=context_payload,
        summary=state.transitions[-1].summary,
        planning_feedback=planning_feedback,
        errors=errors,
        warnings=state.warnings,
    )


def _load_context(
    run_dir: Path,
    *,
    expected_run_id: str,
    accept_manifest_drift: bool = False,
) -> tuple[ExperimentAgentState, ExperimentModuleInput, ImplementationManifest]:
    state = _load_state(run_dir)
    if state.run_id != expected_run_id:
        raise ValueError(
            f"state run_id mismatch: expected {expected_run_id}, got {state.run_id}"
        )
    request = ExperimentModuleInput.model_validate(
        read_json(safe_join(run_dir, state.request_path))
    )
    if _digest_payload(request.model_dump(mode="json")) != state.request_digest:
        raise ValueError("request snapshot digest no longer matches agent state")
    if state.manifest_path is None or state.manifest_digest is None:
        raise ValueError("agent state has no implementation manifest")
    manifest_payload = read_json(safe_join(run_dir, state.manifest_path))
    current_manifest_digest = _digest_payload(manifest_payload)
    if current_manifest_digest != state.manifest_digest:
        if not accept_manifest_drift:
            raise ValueError("manifest snapshot digest no longer matches agent state")
        state.manifest_digest = current_manifest_digest
        state.warnings = _deduplicate(
            [
                *state.warnings,
                "实现清单内容发生变化；活动审查必须重新绑定当前清单",
            ]
        )
    manifest = ImplementationManifest.model_validate(
        _hydrate_manifest_payload(manifest_payload, run_dir)
    )
    if state.datasets_index_path is not None:
        if state.datasets_index_digest is None:
            raise ValueError("agent state has dataset index path without digest")
        index_path = safe_join(run_dir, state.datasets_index_path)
        if _digest_file(index_path) != state.datasets_index_digest:
            raise ValueError("dataset index digest no longer matches agent state")
    if state.implementation_inspection_path is not None:
        if state.implementation_inspection_digest is None:
            raise ValueError("agent state has inspection path without digest")
        inspection_path = safe_join(run_dir, state.implementation_inspection_path)
        if _digest_file(inspection_path) != state.implementation_inspection_digest:
            raise ValueError("implementation inspection digest no longer matches agent state")
    return state, request, manifest


def _verify_dataset_fingerprints(
    state: ExperimentAgentState,
    run_dir: Path,
) -> None:
    if state.datasets_index_path is None:
        raise ValueError("agent state has no dataset index")
    payload = read_json(safe_join(run_dir, state.datasets_index_path))
    if not isinstance(payload, list):
        raise ValueError("dataset index must contain a list")
    by_name = {
        item.get("name"): item
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if set(by_name) != set(state.dataset_paths):
        raise ValueError("dataset index names do not match agent state")
    for name, relative_path in state.dataset_paths.items():
        expected = by_name[name].get("fingerprint")
        actual = dataset_fingerprint(safe_join(run_dir, relative_path))
        if actual != expected:
            raise ValueError(f"dataset fingerprint changed after preparation: {name}")


def _verify_execution_approval(
    state: ExperimentAgentState,
    manifest: ImplementationManifest,
    run_dir: Path,
) -> None:
    if not manifest.execution_approved:
        raise ValueError("execution is blocked because execution_approved=false")
    if (
        manifest.approval_type != "LLM_REVIEW"
        or manifest.approved_by != AgentName.ROOT.value
        or not manifest.reviewer_model
    ):
        raise ValueError("execution approval is not an experiment-agent LLM review")
    if (
        state.implementation_approval_digest is None
        or manifest.approval_digest is None
    ):
        raise ValueError("execution approval is not bound to an implementation digest")
    current = implementation_approval_digest(manifest, run_dir)
    if (
        current != state.implementation_approval_digest
        or current != manifest.approval_digest
    ):
        raise ValueError(
            "implementation code or manifest changed after approval; review and approve again"
        )
    current_code = implementation_code_digest(manifest, run_dir)
    if (
        state.reviewed_code_digest is None
        or manifest.reviewed_code_digest != state.reviewed_code_digest
        or current_code != state.reviewed_code_digest
    ):
        raise ValueError("reviewed code digest no longer matches implementation code")
    if (
        state.smoke_results_path is None
        or state.smoke_results_digest is None
        or manifest.reviewed_smoke_digest != state.smoke_results_digest
        or _digest_file(safe_join(run_dir, state.smoke_results_path))
        != state.smoke_results_digest
    ):
        raise ValueError("reviewed smoke digest no longer matches smoke evidence")
    _load_smoke_results(state, run_dir)
    if (
        state.execution_review_path is None
        or state.execution_review_digest is None
        or _digest_file(safe_join(run_dir, state.execution_review_path))
        != state.execution_review_digest
    ):
        raise ValueError("execution review artifact no longer matches state")
    execution_review = ExecutionReviewDecision.model_validate(
        read_json(safe_join(run_dir, state.execution_review_path))
    )
    if (
        execution_review.decision != "APPROVE_EXECUTION"
        or review_decision_digest(execution_review) != manifest.review_digest
    ):
        raise ValueError("execution review decision is not the bound approval")


def _resolve_resume_run_dir(
    run_dir: Path,
    *,
    base_dir: Path | None,
) -> Path:
    resolved = resolve_run_dir(str(run_dir), base_dir=base_dir)
    state_path = safe_join(resolved, STATE_PATH)
    if not state_path.is_file():
        raise FileNotFoundError(f"agent state not found: {state_path}")
    return resolved


def _load_execution_specs(
    state: ExperimentAgentState,
    run_dir: Path,
) -> list[ExecutionSpec]:
    if (
        state.execution_specs_path is None
        or state.execution_specs_digest is None
    ):
        raise ValueError("agent state has no execution specs")
    path = safe_join(run_dir, state.execution_specs_path)
    if _digest_file(path) != state.execution_specs_digest:
        raise ValueError("execution specs digest no longer matches agent state")
    payload = read_json(path)
    if not isinstance(payload, list):
        raise ValueError("execution specs artifact must contain a list")
    return [ExecutionSpec.model_validate(item) for item in payload]


def _load_runtime_results(
    state: ExperimentAgentState,
    run_dir: Path,
) -> list[RuntimeRunResult]:
    if state.runtime_results_path is None:
        return []
    if state.runtime_results_digest is None:
        raise ValueError("agent state has runtime results path without digest")
    path = safe_join(run_dir, state.runtime_results_path)
    if _digest_file(path) != state.runtime_results_digest:
        raise ValueError("runtime results digest no longer matches agent state")
    payload = read_json(path)
    if not isinstance(payload, list):
        raise ValueError("runtime results artifact must contain a list")
    results = [RuntimeRunResult.model_validate(item) for item in payload]
    specs = _load_execution_specs(state, run_dir)
    specs_by_run_id = {item.run_record_id: item for item in specs}
    result_ids = [item.run_record_id for item in results]
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != set(
        specs_by_run_id
    ):
        raise ValueError(
            "runtime results must contain exactly one result per execution spec"
        )
    for result in results:
        spec = specs_by_run_id[result.run_record_id]
        if (
            result.experiment_id != spec.experiment_id
            or result.method != spec.method
        ):
            raise ValueError(
                f"runtime result identity mismatch: {result.run_record_id}"
            )
        if result.success:
            test_names = [
                item.name for item in result.metrics if item.split == "test"
            ]
            if len(test_names) != len(set(test_names)) or set(
                test_names
            ) != set(spec.metric_names):
                raise ValueError(
                    "successful runtime result metrics do not match execution spec: "
                    f"{result.run_record_id}"
                )
    return results


def _archive_previous_run_state(run_dir: Path, previous_run_id: str) -> Path:
    """把属于旧 run_id 的状态移到 ``run_dir/previous-runs/`` 下并返回归档目录。

    为什么需要：``run_id`` 是规划产物的内容哈希，而 ``run_dir`` 由编排层固定复用。
    REPLAN 会改变规划产物 → ``run_id`` 变化 → 同一个 ``run_dir`` 迎来一个**新** run。
    原实现在这种情况下直接报错，于是新 run 连状态机都初始化不了，整条流水线被判死。

    归档而不是删除：旧状态（输入快照、审查记录、指标）保持可追溯，新 run 也不会
    覆盖它。``data/`` 有意留在原地，下载断点与完成标记必须跨 run 复用。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = safe_join(run_dir, f"{PREVIOUS_RUNS_DIR}/{previous_run_id}-{stamp}")
    archive.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in RUN_STATE_ENTRIES:
        source = run_dir / name
        if not source.exists():
            continue
        destination = archive / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved.append(name)
    write_json_atomic(
        archive / "archive-note.json",
        {
            "archived_run_id": previous_run_id,
            "archived_at_utc": stamp,
            "moved_entries": moved,
            "reason": "run_dir reused by a new run_id after planning changed (REPLAN)",
            "preserved_entries": ["data"],
        },
    )
    return archive


def _load_state(run_dir: Path) -> ExperimentAgentState:
    path = safe_join(run_dir, STATE_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"agent state not found: {path}")
    return ExperimentAgentState.model_validate(read_json(path))


def _save_state(state: ExperimentAgentState, run_dir: Path) -> None:
    validated = ExperimentAgentState.model_validate(state.model_dump(mode="json"))
    write_json_atomic(
        safe_join(run_dir, STATE_PATH),
        validated.model_dump(mode="json"),
    )


def _transition(
    state: ExperimentAgentState,
    target: AgentRunStage,
    *,
    actor: AgentName,
    summary: str,
    artifact_paths: list[str],
) -> None:
    current = state.current_stage
    target_value = target.value
    if target_value not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"illegal agent stage transition: {current} -> {target_value}")
    state.transitions.append(
        AgentTransitionRecord(
            sequence=len(state.transitions) + 1,
            from_stage=current,
            to_stage=target,
            actor=actor,
            summary=summary,
            artifact_paths=artifact_paths,
        )
    )
    state.current_stage = target_value


def _finish_blocked(
    state: ExperimentAgentState,
    request: ExperimentModuleInput,
    run_dir: Path,
    blocker: StageBlocker,
    *,
    actor: AgentName,
) -> None:
    output = _blocked_output(
        request,
        blocker,
        state.warnings,
        state.started_at_epoch,
    )
    write_module_output(output, run_dir)
    state.output_path = MODULE_OUTPUT_PATH
    state.output_digest = _digest_file(safe_join(run_dir, MODULE_OUTPUT_PATH))
    _transition(
        state,
        AgentRunStage.REPLAN,
        actor=actor,
        summary=blocker.reason,
        artifact_paths=[MODULE_OUTPUT_PATH],
    )
    _save_state(state, run_dir)


def _finish_failed(
    state: ExperimentAgentState,
    request: ExperimentModuleInput,
    run_dir: Path,
    runtime_results: list[RuntimeRunResult],
    *,
    errors: list[str],
    actor: AgentName,
) -> None:
    output = ExperimentModuleOutput(
        schema_version=request.schema_version,
        run_id=request.run_id,
        status="FAILED",
        experiment_results=None,
        resource_usage=_resource_usage(state.started_at_epoch, runtime_results),
        reproducibility=None,
        planning_feedback=None,
        warnings=state.warnings,
        errors=errors,
    )
    write_module_output(output, run_dir)
    state.output_path = MODULE_OUTPUT_PATH
    state.output_digest = _digest_file(safe_join(run_dir, MODULE_OUTPUT_PATH))
    _transition(
        state,
        AgentRunStage.FAILED,
        actor=actor,
        summary=errors[0],
        artifact_paths=[MODULE_OUTPUT_PATH, *([RUNTIME_RESULTS_PATH] if runtime_results else [])],
    )
    _save_state(state, run_dir)


def _blocked_output(
    request: ExperimentModuleInput,
    blocker: StageBlocker,
    warnings: list[str],
    started_at_epoch: float,
) -> ExperimentModuleOutput:
    return ExperimentModuleOutput(
        schema_version=request.schema_version,
        run_id=request.run_id,
        status="REPLAN",
        experiment_results=None,
        resource_usage=_resource_usage(started_at_epoch, []),
        reproducibility=None,
        planning_feedback=PlanningFeedback(
            reason=blocker.reason,
            affected_experiment_ids=blocker.affected_experiment_ids,
            blockers=blocker.blockers,
            suggested_changes=blocker.suggested_changes,
        ),
        warnings=_deduplicate(warnings),
        errors=[],
    )


def _resource_usage(
    started_at_epoch: float,
    runtime_results: list[RuntimeRunResult],
) -> ResourceUsage:
    peak_memory = None
    try:
        import psutil

        memory_info = psutil.Process(os.getpid()).memory_info()
        peak_working_set = getattr(memory_info, "peak_wset", None)
        if peak_working_set is not None:
            peak_memory = peak_working_set / (1024**3)
    except ImportError:
        pass
    child_wall_seconds = sum(item.duration_seconds for item in runtime_results)
    return ResourceUsage(
        wall_time_seconds=max(0.0, time.time() - started_at_epoch),
        gpu_hours=sum(
            item.duration_seconds for item in runtime_results if item.uses_gpu
        )
        / 3600,
        cpu_hours=sum(
            item.duration_seconds for item in runtime_results if not item.uses_gpu
        )
        / 3600,
        peak_memory_gb=peak_memory,
        llm_prompt_tokens=0,
        llm_completion_tokens=0,
        estimated_cost=None,
        retry_count=sum(max(0, item.attempts - 1) for item in runtime_results),
    )


def _timeout_seconds(request: ExperimentModuleInput) -> int:
    return (
        request.execution_config.timeout_seconds
        or request.resource_constraints.time_budget_days * 86400
    )


def _attach_smoke_evidence(
    result: RuntimeRunResult,
    run_dir: Path,
) -> RuntimeRunResult:
    candidates = [
        safe_join(run_dir, result.config_path),
        safe_join(run_dir, result.log_path),
    ]
    raw_dir = safe_join(run_dir, f"raw_results/{result.run_record_id}")
    if raw_dir.is_dir():
        candidates.extend(path for path in raw_dir.rglob("*") if path.is_file())
    evidence: list[RuntimeFileEvidence] = []
    for path in sorted(set(candidates)):
        lexical = run_dir / relative_to_run(path, run_dir)
        if lexical.is_symlink() or path.is_symlink():
            raise ValueError("smoke evidence cannot contain symbolic links")
        if not path.is_file():
            continue
        evidence.append(
            RuntimeFileEvidence(
                path=relative_to_run(path, run_dir),
                sha256=_digest_file(path),
                size_bytes=path.stat().st_size,
            )
        )
    if result.success and len(evidence) < 3:
        raise ValueError("successful smoke requires config, log and raw metric evidence")
    return result.model_copy(update={"evidence_files": evidence})


def _verify_smoke_evidence(
    result: RuntimeRunResult,
    run_dir: Path,
) -> None:
    if result.success and len(result.evidence_files) < 3:
        raise ValueError("successful smoke result lacks bound file evidence")
    for evidence in result.evidence_files:
        lexical = run_dir / evidence.path
        if lexical.is_symlink():
            raise ValueError("smoke evidence path became a symbolic link")
        path = safe_join(run_dir, evidence.path)
        if (
            not path.is_file()
            or path.stat().st_size != evidence.size_bytes
            or _digest_file(path) != evidence.sha256
        ):
            raise ValueError(
                f"smoke evidence changed after review: {evidence.path}"
            )


def _bounded_smoke_dataset(
    source: Path,
    run_dir: Path,
    *,
    dataset_name: str,
) -> Path:
    """Return a demonstrably bounded dataset path for non-scientific smoke use."""

    resolved = source.resolve(strict=True)
    relative_to_run(resolved, run_dir)
    entries = [resolved] if resolved.is_file() else list(resolved.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ValueError("bounded smoke dataset rejects symbolic links")
    files = [resolved] if resolved.is_file() else sorted(
        # 下载器会把 .download-complete.json / .download-resume.json 这类点号开头的
        # 记账文件写进数据集目录；它们不是数据文件，却会撞上下面的后缀白名单
        # （.json 不在 _SMOKE_TEXT_SUFFIXES 里），让整个冒烟门禁直接判死。
        path
        for path in entries
        if path.is_file() and not path.name.startswith(".")
    )
    if not files:
        raise ValueError("bounded smoke dataset contains no files")
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes <= _SMOKE_MAX_DATA_BYTES and len(files) <= _SMOKE_MAX_DATA_FILES:
        return resolved
    if (
        len(files) > _SMOKE_MAX_DATA_FILES
        or any(path.suffix.casefold() not in _SMOKE_TEXT_SUFFIXES for path in files)
    ):
        raise ValueError(
            "dataset exceeds bounded smoke scope and cannot be safely sampled; "
            "provide a reviewed implementation-specific small fixture"
        )

    lexical_parts = [
        run_dir / "outputs",
        run_dir / "outputs" / "smoke-data",
        run_dir / "outputs" / "smoke-data" / slugify(dataset_name),
    ]
    if any(path.exists() and path.is_symlink() for path in lexical_parts):
        raise ValueError("smoke-data path cannot contain symbolic links")
    target_root = safe_join(
        run_dir,
        f"outputs/smoke-data/{slugify(dataset_name)}",
    )
    _clear_smoke_target(target_root, run_dir)
    target_root.mkdir(parents=True, exist_ok=False)
    per_file_bytes = max(4096, _SMOKE_MAX_DATA_BYTES // len(files))
    for path in files:
        relative = path.name if resolved.is_file() else path.relative_to(resolved)
        target = safe_join(target_root, Path(relative).as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_bounded_text_copy(path, target, max_bytes=per_file_bytes)
    if resolved.is_file():
        return target_root / resolved.name
    return target_root


def _clear_smoke_target(target: Path, run_dir: Path) -> None:
    relative_to_run(target, run_dir)
    if not target.exists():
        return
    if target.is_symlink():
        raise ValueError("smoke-data target cannot be a symbolic link")
    for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise ValueError("smoke-data target cannot contain symbolic links")
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    target.rmdir()


def _write_bounded_text_copy(
    source: Path,
    target: Path,
    *,
    max_bytes: int,
) -> None:
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    written = 0
    lines = 0
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            for line in reader:
                if lines >= _SMOKE_MAX_TEXT_LINES:
                    break
                remaining = max_bytes - written
                if remaining <= 0:
                    break
                if len(line) > remaining:
                    if lines == 0:
                        raise ValueError("first smoke-data row exceeds bounded byte limit")
                    break
                writer.write(line)
                written += len(line)
                lines += 1
        if lines < 2:
            raise ValueError("bounded smoke-data sample requires at least two rows")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _smoke_data_access(
    results: list[RuntimeRunResult],
    run_dir: Path,
) -> dict[str, list[str]]:
    access: dict[str, list[str]] = {}
    for result in results:
        config = read_json(safe_join(run_dir, result.config_path))
        dataset_path = config.get("dataset_path") if isinstance(config, dict) else None
        if not isinstance(dataset_path, str):
            raise ValueError("smoke config does not declare dataset_path")
        access.setdefault(result.method, []).append(dataset_path)
    return {method: list(dict.fromkeys(paths)) for method, paths in access.items()}


def _metric_implementations(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
) -> list[MetricImplementation]:
    return [
        MetricImplementation(
            name=name,
            **manifest.metrics[name].model_dump(mode="python", exclude={"verified"}),
        )
        for name in request.experiment_plan.metrics
    ]


def _baseline_implementations(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
) -> list[BaselineImplementation]:
    implementations: list[BaselineImplementation] = []
    for baseline in request.experiment_plan.baselines:
        definition = manifest.implementations[baseline.name]
        implementations.append(
            BaselineImplementation(
                name=baseline.name,
                paper_id=baseline.paper_id,
                implementation_url=definition.implementation_url,
                revision=definition.revision,
                entry_command=portable_command(
                    definition.command, run_dir=run_dir
                ),
                implementation_notes=definition.notes,
            )
        )
    return implementations


def _read_execution_progress(run_dir: Path) -> dict[str, object] | None:
    path = safe_join(run_dir, MONITOR_PATH)
    if not path.is_file() or path.is_symlink():
        return None
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("execution monitor must contain a JSON object")
    return payload


def _initialize_response(
    state: ExperimentAgentState,
    run_dir: Path,
) -> InitializeResponse:
    if state.current_stage in _TERMINAL_STAGES:
        _read_output(run_dir)
    next_agent = {
        AgentRunStage.INITIALIZED.value: AgentName.DATA,
        AgentRunStage.DATA_READY.value: AgentName.IMPLEMENTATION,
        AgentRunStage.CODE_REVIEW_REQUIRED.value: AgentName.ROOT,
        AgentRunStage.EXECUTION_REVIEW_REQUIRED.value: AgentName.ROOT,
        AgentRunStage.IMPLEMENTATION_READY.value: AgentName.EXECUTION,
        AgentRunStage.EXECUTION_READY.value: AgentName.ANALYSIS,
        AgentRunStage.COMPLETED.value: AgentName.MODULE_FOUR,
        AgentRunStage.REPLAN.value: AgentName.MODULE_TWO,
    }.get(state.current_stage)
    planning_feedback, errors = _terminal_details(state, run_dir)
    execution_progress = _read_execution_progress(run_dir)
    return InitializeResponse(
        run_id=state.run_id,
        run_dir=str(run_dir),
        stage=state.current_stage,
        terminal=state.current_stage in _TERMINAL_STAGES,
        next_agent=next_agent,
        summary=state.transitions[-1].summary,
        output_path=state.output_path,
        artifact_manifest_path=state.artifact_manifest_path,
        planning_feedback=planning_feedback,
        errors=errors,
        warnings=state.warnings,
        execution_monitor_path=(MONITOR_PATH if execution_progress is not None else None),
        execution_progress=execution_progress,
    )


def _in_progress_response(
    state: ExperimentAgentState,
    run_dir: Path,
    progress: DownloadProgress,
) -> DataAgentResponse:
    """下载未完成但可续跑时的响应。

    与 ``_data_response`` 分开写，因为后者依赖 ``state.transitions[-1]``——首次进入
    时还没有任何 transition，会 IndexError。这里显式构造：stage 停在 INITIALIZED、
    ``terminal=False``，让 agent 知道"再调一次"而不是"这轮结束了"。
    """
    return DataAgentResponse(
        run_id=state.run_id,
        run_dir=str(run_dir),
        consumer=_consumer_for_stage(state.current_stage),
        stage=state.current_stage,
        terminal=False,
        prepared_dataset_count=len(state.dataset_paths),
        datasets_index_path=None,
        summary=(
            f"数据下载进行中（{len(progress.completed_files)}/{progress.total_files} 个文件，"
            f"已落盘 {progress.bytes_on_disk} 字节）；"
            f"{progress.resume_hint}"
        ),
        download_progress=progress,
        warnings=state.warnings,
    )


def _data_response(
    state: ExperimentAgentState,
    run_dir: Path,
) -> DataAgentResponse:
    planning_feedback, errors = _terminal_details(state, run_dir)
    return DataAgentResponse(
        run_id=state.run_id,
        run_dir=str(run_dir),
        consumer=_consumer_for_stage(state.current_stage),
        stage=state.current_stage,
        terminal=state.current_stage in _TERMINAL_STAGES,
        prepared_dataset_count=len(state.dataset_paths),
        datasets_index_path=(
            state.datasets_index_path if state.dataset_paths else None
        ),
        summary=state.transitions[-1].summary,
        planning_feedback=planning_feedback,
        errors=errors,
        warnings=state.warnings,
    )


def _implementation_response(
    state: ExperimentAgentState,
    run_dir: Path,
) -> ImplementationAgentResponse:
    count = 0
    if state.execution_specs_path:
        payload = read_json(safe_join(run_dir, state.execution_specs_path))
        count = len(payload) if isinstance(payload, list) else 0
    planning_feedback, errors = _terminal_details(state, run_dir)
    inspection_payload = None
    if state.implementation_inspection_path is not None:
        inspection_path = safe_join(run_dir, state.implementation_inspection_path)
        if (
            state.implementation_inspection_digest is None
            or _digest_file(inspection_path) != state.implementation_inspection_digest
        ):
            raise ValueError("implementation inspection digest no longer matches state")
        inspection_payload = ImplementationInspection.model_validate(
            read_json(inspection_path)
        ).model_dump(mode="json")
    return ImplementationAgentResponse(
        run_id=state.run_id,
        run_dir=str(run_dir),
        consumer=_consumer_for_stage(state.current_stage),
        stage=state.current_stage,
        terminal=state.current_stage in _TERMINAL_STAGES,
        execution_spec_count=count,
        execution_specs_path=state.execution_specs_path,
        implementation_review_path=state.implementation_review_path,
        implementation_inspection_path=state.implementation_inspection_path,
        inspection=inspection_payload,
        approval_required=(
            state.current_stage
            in {
                AgentRunStage.CODE_REVIEW_REQUIRED.value,
                AgentRunStage.EXECUTION_REVIEW_REQUIRED.value,
            }
        ),
        review_phase=(
            state.current_stage
            if state.current_stage
            in {
                AgentRunStage.CODE_REVIEW_REQUIRED.value,
                AgentRunStage.EXECUTION_REVIEW_REQUIRED.value,
            }
            else None
        ),
        summary=state.transitions[-1].summary,
        planning_feedback=planning_feedback,
        errors=errors,
        warnings=state.warnings,
    )


def _execution_response(
    state: ExperimentAgentState,
    run_dir: Path,
) -> ExecutionAgentResponse:
    runtime_results = _load_runtime_results(state, run_dir)
    successful = sum(item.success for item in runtime_results)
    failed = len(runtime_results) - successful
    planning_feedback, errors = _terminal_details(state, run_dir)
    execution_progress = _read_execution_progress(run_dir)
    return ExecutionAgentResponse(
        run_id=state.run_id,
        run_dir=str(run_dir),
        consumer=_consumer_for_stage(state.current_stage),
        stage=state.current_stage,
        terminal=state.current_stage in _TERMINAL_STAGES,
        successful_run_count=successful,
        failed_run_count=failed,
        runtime_results_path=state.runtime_results_path,
        execution_monitor_path=(MONITOR_PATH if execution_progress is not None else None),
        execution_events_path=(
            EVENTS_PATH if safe_join(run_dir, EVENTS_PATH).is_file() else None
        ),
        execution_progress=execution_progress,
        summary=state.transitions[-1].summary,
        planning_feedback=planning_feedback,
        errors=errors,
        warnings=state.warnings,
    )


def _analysis_response(
    state: ExperimentAgentState,
    run_dir: Path,
) -> AnalysisAgentResponse:
    module_status = None
    candidate_count = 0
    if state.output_path:
        output = _read_output(run_dir)
        module_status = output.status
        if output.experiment_results is not None:
            candidate_count = len(output.experiment_results.visualization_candidates)
    planning_feedback, errors = _terminal_details(state, run_dir)
    return AnalysisAgentResponse(
        run_id=state.run_id,
        run_dir=str(run_dir),
        consumer=_consumer_for_stage(state.current_stage),
        stage=state.current_stage,
        terminal=state.current_stage in _TERMINAL_STAGES,
        module_status=module_status,
        output_path=state.output_path,
        artifact_manifest_path=state.artifact_manifest_path,
        visualization_candidate_count=candidate_count,
        summary=state.transitions[-1].summary,
        planning_feedback=planning_feedback,
        errors=errors,
        warnings=state.warnings,
    )


def _read_output(run_dir: Path) -> ExperimentModuleOutput:
    state = _load_state(run_dir)
    if state.output_path is None or state.output_digest is None:
        raise ValueError("agent state has no output path/digest")
    path = safe_join(run_dir, state.output_path)
    if not path.is_file():
        raise FileNotFoundError(f"module output not found: {path}")
    if _digest_file(path) != state.output_digest:
        raise ValueError("module output digest no longer matches agent state")
    output = ExperimentModuleOutput.model_validate(read_json(path))
    if state.current_stage == AgentRunStage.COMPLETED.value:
        _verify_artifact_manifest(state, run_dir)
    return output


def _write_artifact_manifest(
    output: ExperimentModuleOutput,
    state: ExperimentAgentState,
    run_dir: Path,
) -> Path:
    paths = {
        MODULE_OUTPUT_PATH,
        "outputs/summary.md",
        state.request_path,
        state.manifest_path,
        state.datasets_index_path,
        state.implementation_review_path,
        state.implementation_inspection_path,
        state.code_review_context_path,
        state.code_review_path,
        state.smoke_results_path,
        state.execution_review_context_path,
        state.execution_review_path,
        state.execution_specs_path,
        state.runtime_results_path,
        state.environment_deployment_path,
    }
    if output.reproducibility is not None:
        paths.add(output.reproducibility.environment_path)
        paths.add(output.reproducibility.environment_deployment_path)
        paths.add(output.reproducibility.results_csv_path)
        paths.add(output.reproducibility.execution_monitor_path)
        paths.add(output.reproducibility.execution_events_path)
    if output.experiment_results is not None:
        results = output.experiment_results
        paths.update(item.path for item in results.visualization_data)
        paths.update(item.path for item in results.table_artifacts)
        paths.update(item.path for item in results.figure_artifacts)
        paths.add(results.writing_brief_path)
        for candidate in results.visualization_candidates:
            paths.add(candidate.preview_path)
            paths.add(candidate.editable_spec_path)
            export_paths = candidate.design_spec.get("export_paths", {})
            if isinstance(export_paths, dict):
                paths.update(
                    path for path in export_paths.values() if isinstance(path, str)
                )
            qa_path = candidate.design_spec.get("qa_path")
            if isinstance(qa_path, str):
                paths.add(qa_path)

    artifacts: list[dict[str, Any]] = []
    for relative_path in sorted(path for path in paths if path is not None):
        if relative_path == ARTIFACT_MANIFEST_PATH:
            raise ValueError("artifact manifest cannot list itself")
        absolute_path = safe_join(run_dir, relative_path)
        if not absolute_path.is_file():
            raise FileNotFoundError(
                f"declared delivery artifact does not exist: {relative_path}"
            )
        artifacts.append(
            {
                "path": relative_path.replace("\\", "/"),
                "sha256": _digest_file(absolute_path),
                "size_bytes": absolute_path.stat().st_size,
            }
        )
    manifest_path = safe_join(run_dir, ARTIFACT_MANIFEST_PATH)
    write_json_atomic(
        manifest_path,
        {
            "schema_version": "1.0.0",
            "run_id": output.run_id,
            "artifacts": artifacts,
        },
    )
    return manifest_path


def _verify_artifact_manifest(
    state: ExperimentAgentState,
    run_dir: Path,
) -> None:
    if (
        state.artifact_manifest_path is None
        or state.artifact_manifest_digest is None
    ):
        raise ValueError("completed state has no artifact manifest")
    manifest_path = safe_join(run_dir, state.artifact_manifest_path)
    if _digest_file(manifest_path) != state.artifact_manifest_digest:
        raise ValueError("artifact manifest digest no longer matches agent state")
    payload = read_json(manifest_path)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "run_id",
        "artifacts",
    }:
        raise ValueError("artifact manifest has an invalid structure")
    if payload["schema_version"] != "1.0.0" or payload["run_id"] != state.run_id:
        raise ValueError("artifact manifest identity does not match agent state")
    items = payload["artifacts"]
    if not isinstance(items, list) or not items:
        raise ValueError("artifact manifest must contain artifacts")
    seen_paths: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise ValueError("artifact manifest entry has an invalid structure")
        relative_path = item["path"]
        digest = item["sha256"]
        size_bytes = item["size_bytes"]
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("artifact manifest path must be a non-empty string")
        if relative_path in seen_paths or relative_path == ARTIFACT_MANIFEST_PATH:
            raise ValueError(f"artifact manifest path is duplicated or reserved: {relative_path}")
        seen_paths.add(relative_path)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"artifact manifest digest is invalid: {relative_path}")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            raise ValueError(f"artifact manifest size is invalid: {relative_path}")
        path = safe_join(run_dir, relative_path)
        if path.stat().st_size != size_bytes or _digest_file(path) != digest:
            raise ValueError(f"delivery artifact no longer matches manifest: {relative_path}")


def _terminal_details(
    state: ExperimentAgentState,
    run_dir: Path,
) -> tuple[PlanningFeedback | None, list[str]]:
    if state.current_stage not in {
        AgentRunStage.REPLAN.value,
        AgentRunStage.FAILED.value,
    }:
        return None, []
    output = _read_output(run_dir)
    return output.planning_feedback, output.errors


def _digest_payload(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_file(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"artifact not found for digest: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hydrate_manifest_payload(payload: Any, run_dir: Path) -> Any:
    replacements = {
        "{python}": sys.executable,
        "{run_dir}": str(run_dir),
        "{user_home}": str(Path.home()),
    }

    def hydrate(value: Any) -> Any:
        if isinstance(value, str):
            for token, replacement in replacements.items():
                value = value.replace(token, replacement)
            return value
        if isinstance(value, list):
            return [hydrate(item) for item in value]
        if isinstance(value, dict):
            return {key: hydrate(item) for key, item in value.items()}
        return value

    return hydrate(payload)


def _stage_rank(stage: str) -> int:
    order = {
        AgentRunStage.INITIALIZED.value: 0,
        AgentRunStage.DATA_READY.value: 1,
        AgentRunStage.CODE_REVIEW_REQUIRED.value: 2,
        AgentRunStage.EXECUTION_REVIEW_REQUIRED.value: 3,
        AgentRunStage.IMPLEMENTATION_READY.value: 4,
        AgentRunStage.EXECUTION_READY.value: 5,
        AgentRunStage.COMPLETED.value: 6,
        AgentRunStage.REPLAN.value: 7,
        AgentRunStage.FAILED.value: 7,
    }
    return order[stage]


def _consumer_for_stage(stage: str) -> AgentName:
    consumers = {
        AgentRunStage.INITIALIZED.value: AgentName.DATA,
        AgentRunStage.DATA_READY.value: AgentName.IMPLEMENTATION,
        AgentRunStage.CODE_REVIEW_REQUIRED.value: AgentName.ROOT,
        AgentRunStage.EXECUTION_REVIEW_REQUIRED.value: AgentName.ROOT,
        AgentRunStage.IMPLEMENTATION_READY.value: AgentName.EXECUTION,
        AgentRunStage.EXECUTION_READY.value: AgentName.ANALYSIS,
        AgentRunStage.COMPLETED.value: AgentName.MODULE_FOUR,
        AgentRunStage.REPLAN.value: AgentName.MODULE_TWO,
        AgentRunStage.FAILED.value: AgentName.ROOT,
    }
    return consumers[stage]


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


@contextmanager
def _stage_lock(run_dir: Path) -> Iterator[None]:
    lock = safe_join(run_dir, "outputs/.agent-stage.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{time.time_ns()}"
    _acquire_stage_lock(lock, token)
    try:
        yield
    finally:
        try:
            payload = read_json(lock)
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("token") == token:
            lock.unlink(missing_ok=True)


def _acquire_stage_lock(lock: Path, token: str) -> None:
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "created_at_epoch": time.time(),
            "token": token,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    for _attempt in range(2):
        try:
            descriptor = os.open(
                lock,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            if _stage_lock_is_stale(lock):
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise RuntimeError(
                "another ExperimentAgent stage is already using this run_dir"
            )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        return
    raise RuntimeError("could not recover the stale ExperimentAgent stage lock")


def _stage_lock_is_stale(lock: Path) -> bool:
    try:
        payload = read_json(lock)
        pid = int(payload["pid"])
        created_at = float(payload.get("created_at_epoch", 0.0))
        # Stage critical sections are filesystem-only and intentionally short.
        # A lock older than the recovery window is stale even if its PID is a
        # long-lived JiuwenSwarm host process that no longer owns the section.
        if created_at and time.time() - created_at > 60:
            return True
    except (OSError, ValueError, KeyError, TypeError):
        try:
            return time.time() - lock.stat().st_mtime > 60
        except OSError:
            return True
    if pid == os.getpid():
        return False
    try:
        import psutil
    except ImportError:
        return not _process_exists_without_psutil(pid)
    return not psutil.pid_exists(pid)


def _process_exists_without_psutil(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
