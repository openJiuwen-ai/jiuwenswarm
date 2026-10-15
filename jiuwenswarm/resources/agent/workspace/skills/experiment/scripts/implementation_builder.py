"""Deterministic Implementation Builder used by the implementation subagent.

It can reuse reviewed entries and materialize a small curated sklearn runner.
Arbitrary generated source may be written only through the scoped proposal
function; it is never marked ready or executed automatically.
"""

from __future__ import annotations

import csv
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import Field, HttpUrl, field_validator, model_validator

from contracts import (
    STANDARD_EXECUTOR_TASK_TYPES,
    TASK_REQUIRED_FIELD_FAMILIES,
    ExperimentModuleInput,
    TaskType,
    resolve_task_type,
)
from retrieval_metrics import RETRIEVAL_METRICS, available_metric_names
from io_utils import (
    portable_arguments,
    relative_to_run,
    safe_join,
    slugify,
    write_json_atomic,
    write_text_atomic,
)
from plan_execution import planned_method_names
from run_experiments import parse_metrics
from runtime_models import (
    ImplementationDefinition,
    ImplementationManifest,
    MetricDefinition,
    RuntimeModel,
    StageBlocker,
)


_METHOD_ALIASES: dict[str, tuple[str, set[str]]] = {
    "random_forest": (
        "random_forest",
        {"random forest", "random_forest", "randomforestclassifier", "randomforestclassifier", "rf", "随机森林"},
    ),
    "logistic_regression": (
        "logistic_regression",
        {"logistic regression", "logistic_regression", "logisticregression", "lr", "逻辑回归"},
    ),
    "svm": ("svm", {"svm", "svc", "svr", "support vector machine", "支持向量机"}),
    "decision_tree": (
        "decision_tree",
        {"decision tree", "decision_tree", "decisiontreeclassifier", "decisiontreeregressor", "dt", "决策树"},
    ),
    "xgboost": ("xgboost", {"xgboost", "xgb", "xgboost classifier", "xgboost regressor"}),
}
_FRAMEWORK_MODEL_MARKERS = {
    "pytorch",
    "torch",
    "transformer",
    "transformers",
    "bert",
    "roberta",
    "resnet",
    "vit",
}

_CLASSIFICATION_METRICS = {
    "accuracy": ("accuracy_score", {}, "MAXIMIZE", "score"),
    "top-1_acc": ("accuracy_score", {}, "MAXIMIZE", "score"),
    "macro_f1": ("f1_score", {"average": "macro"}, "MAXIMIZE", "score"),
    "micro_f1": ("f1_score", {"average": "micro"}, "MAXIMIZE", "score"),
    "weighted_f1": ("f1_score", {"average": "weighted"}, "MAXIMIZE", "score"),
    "train_time_seconds": ("train_time_seconds", {}, "MINIMIZE", "seconds"),
    "inference_time_seconds": ("inference_time_seconds", {}, "MINIMIZE", "seconds"),
    "total_time_seconds": ("total_time_seconds", {}, "MINIMIZE", "seconds"),
}
_REGRESSION_METRICS = {
    "mae": ("mean_absolute_error", {}, "MINIMIZE", None),
    "mse": ("mean_squared_error", {}, "MINIMIZE", None),
    "rmse": ("root_mean_squared_error", {}, "MINIMIZE", None),
    "r2": ("r2_score", {}, "MAXIMIZE", "score"),
}
_MEMORY_COMPRESSION_METRICS = {
    "compression_ratio": ("original_token_count / compressed_token_count per example, then arithmetic mean", "MAXIMIZE", "times"),
    "qa_token_f1": ("token F1 between the deterministic extractive reader output and the gold answer", "MAXIMIZE", "score"),
    "answer_exact_match": ("normalized exact match between the deterministic extractive reader output and the gold answer", "MAXIMIZE", "score"),
    "original_token_count": ("mean regex-token count of the uncompressed context", "MINIMIZE", "tokens"),
    "compressed_token_count": ("mean regex-token count after the selected compressor", "MINIMIZE", "tokens"),
}
_MEMORY_COMPRESSION_METHODS = {
    "uncompressed_context",
    "recent_window_20pct",
    "query_focused_20pct",
    "loss_aware_hierarchical_20pct",
}
#: 检索标准指标的实现模块（`manifest.metrics[].implementation` 里声明的入口）。
#: 生成 runner 时按这个名字 import。
RETRIEVAL_METRIC_MODULE = "retrieval_metrics"

_ALLOWED_GENERATED_FILES = {
    "main.py",
    "README.md",
    "requirements.txt",
    "test_smoke.py",
    "config.example.json",
}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_INLINE_SECRET = re.compile(
    r"(?i)['\"]?(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"token|secret|password|private[_-]?key|authorization)['\"]?\s*[:=]\s*"
    r"['\"][^'\"]+['\"]"
)
_PINNED_DEPENDENCY = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.+!-]*$"
)


class ImplementationReviewItem(RuntimeModel):
    method: str = Field(min_length=1)
    action: str = Field(pattern="^(REUSE|BUILD|GENERATED|BLOCKED)$")
    code_path: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    implementation_url: str | None = None
    revision: str | None = None
    license: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    command: list[str] = Field(min_length=1)
    data_access: list[str] = Field(min_length=1)
    resource_requirements: dict[str, Any]
    smoke_test: dict[str, Any]
    ready: bool
    risks: list[str] = Field(default_factory=list)


class ImplementationReview(RuntimeModel):
    schema_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    phase: str = "REQUEST_APPROVAL"
    workflow: list[str] = [
        "inspect",
        "reuse",
        "build",
        "test",
        "register",
        "request_approval",
    ]
    task_type: str | None = None
    label_column: str | None = None
    metric_names: list[str] = Field(min_length=1)
    items: list[ImplementationReviewItem] = Field(min_length=1)
    ready_for_approval: bool
    execution_approved: bool
    approval_notice: str = Field(min_length=1)


class ImplementationInspection(RuntimeModel):
    schema_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    research_goal: str = Field(min_length=1)
    components: list[dict[str, Any]] = Field(min_length=1)
    technical_route: str = Field(min_length=1)
    algorithm_reference: list[str]
    domain: dict[str, Any]
    resource_constraints: dict[str, Any]
    datasets: list[dict[str, Any]] = Field(min_length=1)
    dataset_paths: dict[str, str] = Field(min_length=1)
    dataset_structure: list[dict[str, Any]] = Field(min_length=1)
    split_strategy: dict[str, Any]
    preprocessing_pipeline: list[str] = Field(min_length=1)
    task_type: str | None = None
    label_column: str | None = None
    primary_experiments: list[str] = Field(min_length=1)
    baselines: list[dict[str, Any]] = Field(min_length=1)
    ablations: list[dict[str, Any]]
    metrics: list[str] = Field(min_length=1)
    planned_methods: list[str] = Field(min_length=1)
    existing_implementations: dict[str, dict[str, Any]]
    python_executable: str = Field(min_length=1)
    installed_optional_runtimes: dict[str, str | None]


class GeneratedImplementationProposal(RuntimeModel):
    method: str = Field(min_length=1)
    files: dict[str, str] = Field(min_length=1)
    source_url: HttpUrl | None
    revision: str | None
    license: str = Field(min_length=1)
    dependency_plan: list[str] = Field(default_factory=list)
    required_env: list[str] = Field(default_factory=list)
    uses_gpu: bool = False
    notes: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def accept_direct_file_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        direct_names = _ALLOWED_GENERATED_FILES & set(value)
        if not direct_names:
            return value
        if "files" in value:
            raise ValueError("proposal cannot mix files with direct filename fields")
        normalized = dict(value)
        normalized["files"] = {
            name: normalized.pop(name) for name in sorted(direct_names)
        }
        return normalized

    @field_validator("files")
    @classmethod
    def files_are_scoped(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - _ALLOWED_GENERATED_FILES
        if unknown:
            raise ValueError(f"generated proposal contains disallowed files: {sorted(unknown)}")
        if "main.py" not in value or "README.md" not in value or "requirements.txt" not in value:
            raise ValueError("generated proposal requires main.py, README.md and requirements.txt")
        if any(len(content.encode("utf-8")) > 256_000 for content in value.values()):
            raise ValueError("each generated source file must be at most 256 KiB")
        if any(_INLINE_SECRET.search(content) for content in value.values()):
            raise ValueError("generated source appears to contain an inline secret")
        required_markers = {
            "--config",
            "--metrics",
            "dataset_path",
            "seed",
            "split_strategy",
            "preprocessing_pipeline",
            "metric_names",
        }
        missing = sorted(marker for marker in required_markers if marker not in value["main.py"])
        if missing:
            raise ValueError(
                "generated main.py does not implement the unified entry contract: "
                f"missing {missing}"
            )
        return value

    @field_validator("dependency_plan")
    @classmethod
    def dependencies_are_pinned(cls, values: list[str]) -> list[str]:
        unpinned = [
            value for value in values if not _PINNED_DEPENDENCY.fullmatch(value)
        ]
        if unpinned:
            raise ValueError(
                "new dependency plans require exact versions: " + ", ".join(unpinned)
            )
        if len(values) != len(set(values)):
            raise ValueError("dependency_plan cannot contain duplicate packages")
        return values

    @field_validator("required_env")
    @classmethod
    def environment_contains_names_only(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not _ENV_NAME.fullmatch(v) for v in values):
            raise ValueError("required_env must contain unique uppercase variable names")
        return values

    @model_validator(mode="after")
    def requirements_match_install_plan(self) -> "GeneratedImplementationProposal":
        lines = [
            line.strip()
            for line in self.files["requirements.txt"].splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if lines != self.dependency_plan:
            raise ValueError(
                "requirements.txt must exactly match dependency_plan in order"
            )
        if self.source_url is not None and not (
            isinstance(self.revision, str) and self.revision.strip()
        ):
            raise ValueError("source_url requires a fixed revision")
        if self.source_url is not None and (
            self.source_url.username is not None or self.source_url.password is not None
        ):
            raise ValueError("source_url cannot contain embedded credentials")
        if self.source_url is not None and self.license.strip().casefold() in {
            "unknown",
            "tbd",
            "unconfirmed",
            "none",
            "n/a",
        }:
            raise ValueError("open-source proposals require a confirmed license")
        return self


def inspect_implementation_context(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
    dataset_paths: dict[str, str],
) -> ImplementationInspection:
    """Expose exactly the context the Builder may use, without changing state."""

    task_type, label_column, _unrecognized = _explicit_task_metadata(request)
    return ImplementationInspection(
        run_id=request.run_id,
        research_goal=request.method_design.research_goal,
        components=[
            item.model_dump(mode="json") for item in request.method_design.components
        ],
        technical_route=request.method_design.technical_route,
        algorithm_reference=request.method_design.algorithm_reference,
        domain=request.domain.model_dump(mode="json"),
        resource_constraints=request.resource_constraints.model_dump(mode="json"),
        datasets=[
            item.model_dump(mode="json") for item in request.data_plan.datasets
        ],
        dataset_paths={
            name: relative_to_run(Path(path), run_dir)
            for name, path in dataset_paths.items()
        },
        dataset_structure=[
            _dataset_structure_summary(
                name,
                Path(path),
                task_type=task_type,
                label_column=label_column,
            )
            for name, path in dataset_paths.items()
        ],
        split_strategy=request.data_plan.split_strategy,
        preprocessing_pipeline=request.data_plan.preprocessing_pipeline,
        task_type=task_type,
        label_column=label_column,
        primary_experiments=request.experiment_plan.primary_experiments,
        baselines=[
            item.model_dump(mode="json")
            for item in request.experiment_plan.baselines
        ],
        ablations=[
            item.model_dump(mode="json")
            for item in request.experiment_plan.ablation_plan
        ],
        metrics=request.experiment_plan.metrics,
        planned_methods=sorted(planned_method_names(request)),
        existing_implementations={
            name: {
                "ready": definition.ready,
                "source_kind": definition.source_kind,
                "implementation_url": (
                    str(definition.implementation_url)
                    if definition.implementation_url is not None
                    else None
                ),
                "revision": definition.revision,
                "license": definition.license,
                "dependency_plan": definition.dependency_plan,
                "required_env": definition.required_env,
                "uses_gpu": definition.uses_gpu,
            }
            for name, definition in manifest.implementations.items()
        },
        python_executable=portable_arguments([sys.executable], run_dir=run_dir)[0],
        installed_optional_runtimes={
            name: _package_version(name)
            for name in (
                "pandas",
                "scikit-learn",
                "xgboost",
                "torch",
                "transformers",
            )
        },
    )


def build_implementations(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
    dataset_paths: dict[str, str],
) -> tuple[ImplementationManifest, ImplementationReview | None, StageBlocker | None]:
    """Inspect, reuse/build, test and register required implementations."""

    manifest_payload = manifest.model_dump(mode="python")
    task_type, label_column, unrecognized_task = _explicit_task_metadata(request)
    methods = sorted(planned_method_names(request))
    items: list[ImplementationReviewItem] = []
    blockers: list[str] = []
    suggestions: list[str] = []

    for index, ablation in enumerate(request.experiment_plan.ablation_plan):
        if ablation.removed_by not in methods:
            # 显式写 [experiment]：这是 experiment_plan 字段的问题，修法在 planner 侧。
            # 不显式标就会被关键词规则按"实现"判成 [method]，把 REPLAN 路由去重跑
            # method-designer——而那条路由改不到 experiment_plan（见 contracts.py
            # categorize_blocker 的同款注释与 2026-09-17 事故记录）。
            blockers.append(
                f"[experiment] experiment_plan.ablation_plan[{index}].removed_by="
                f"{ablation.removed_by!r}未精确登记为实验矩阵实现"
            )
            suggestions.append(
                "为每个消融提供独立实验ID和可执行实现名；不得从自然语言猜测消融代码"
                "（removed_by 必须逐字符等于矩阵第3列起不含 `=` 的某个实现名）"
            )

    for metric in request.experiment_plan.metrics:
        current = manifest.metrics.get(metric)
        if current is not None and current.verified:
            continue
        if task_type is None:
            blockers.append(_task_type_blocker(unrecognized_task, where="指标计算"))
            suggestions.append(_TASK_TYPE_SUGGESTION)
            continue
        definition = _standard_metric_definition(metric, task_type)
        if definition is None:
            blockers.append(
                f"[metric] 指标{metric!r}在task_type={task_type!r}下没有无歧义的"
                "确定性定义。任务类型已经识别，缺少的是该指标的可执行语义，不是"
                "数据下载或任务执行器。"
                f"可用指标名：{_available_metric_names(task_type)}。"
            )
            suggestions.append(
                f"把 {metric!r} 改为与task_type={task_type!r}匹配的标准指标并同步"
                "success_criteria、baselines[].metric_name和创新点evidence_metric。"
                "当前运行时没有QA/混合任务的受控指标执行器；自然语言"
                "metric_definitions不会自动获得verified=true，也不能把accuracy偷偷"
                "等同于hit_rate@1。"
            )
            continue
        manifest_payload["metrics"][metric] = definition.model_dump(mode="python")

    for method in methods:
        existing = manifest.implementations.get(method)
        if existing is not None and existing.ready:
            if existing.implementation_url is not None and (
                not existing.revision or not existing.license
            ):
                blockers.append(
                    f"开源实现{method!r}缺少固定revision或许可信息"
                )
                suggestions.append(
                    f"为{method!r}登记可核验的revision和license后再复用"
                )
                continue
            items.append(
                _review_existing(method, existing, request, dataset_paths, run_dir)
            )
            continue
        if existing is not None and existing.source_kind == "GENERATED":
            items.append(
                ImplementationReviewItem(
                    method=method,
                    action="GENERATED",
                    code_path=f"implementations/{slugify(method)}",
                    source_kind=existing.source_kind,
                    implementation_url=(
                        str(existing.implementation_url)
                        if existing.implementation_url is not None
                        else None
                    ),
                    revision=existing.revision,
                    license=existing.license,
                    dependencies=existing.dependency_plan,
                    command=portable_arguments(existing.command, run_dir=run_dir),
                    data_access=[
                        relative_to_run(Path(path), run_dir)
                        for path in dataset_paths.values()
                    ],
                    resource_requirements={
                        "uses_gpu": existing.uses_gpu,
                        "gpu_hours_limit": request.resource_constraints.gpu_hours,
                        "memory_gb_limit": request.resource_constraints.memory_gb,
                    },
                    smoke_test={
                        "passed": False,
                        "command": [],
                        "detail": "等待根experiment-agent第一阶段独立审查后授权受控冒烟测试",
                    },
                    ready=False,
                    risks=["LLM生成代码尚未执行；必须通过根Agent独立审查与确定性门禁"],
                )
            )
            continue
        if task_type == TaskType.MEMORY_COMPRESSION.value:
            if method not in _MEMORY_COMPRESSION_METHODS:
                blockers.append(
                    f"[method] memory_compression 方法 {method!r} 没有固定版本执行语义；"
                    f"可执行方法名为 {sorted(_MEMORY_COMPRESSION_METHODS)}"
                )
                suggestions.append(
                    "把 experiment_matrix、baselines[].name 与主方法名改为执行能力契约发布的 method_ids；"
                    "不得仅按相似名称猜测压缩算法。"
                )
                continue
            dataset_error = _validate_memory_compression_dataset(dataset_paths)
            if dataset_error is not None:
                blockers.append(dataset_error)
                suggestions.append(
                    "提供 JSON/JSONL 数据文件，并保证每条样本可解析出 context/history/memory、"
                    "question/query/input 与 answer/gold_answer/answers。"
                )
                continue
            definition, item = _build_memory_compression_executor(
                request, method, run_dir, dataset_paths,
            )
            manifest_payload["implementations"][method] = definition.model_dump(mode="python")
            items.append(item)
            continue
        family = _standard_family(method)
        if family is None:
            if any(
                marker in method.casefold()
                for marker in _FRAMEWORK_MODEL_MARKERS
            ):
                blockers.append(
                    f"常见框架模型{method!r}尚无经过测试且固定revision的实现登记"
                )
                suggestions.append(
                    f"优先登记{method!r}的受测PyTorch/Transformers通用执行器、"
                    "模型标识、revision、license和数据适配；不要临时猜架构"
                )
            else:
                blockers.append(f"方法{method!r}没有可复用实现，也不属于受控标准执行器")
                suggestions.append(
                    f"补充{method!r}的方法细节、固定来源/revision/许可，"
                    "或通过Implementation Agent的受限write_generated动作提交实现"
                )
            continue
        if task_type is None:
            blockers.append(_task_type_blocker(unrecognized_task, where=f"方法{method!r}"))
            suggestions.append(_TASK_TYPE_SUGGESTION)
            continue
        if task_type not in STANDARD_EXECUTOR_TASK_TYPES:
            # 任务类型认识、但没有内置执行器。区分两种情况：
            #   - 该任务确实不该用标准 sklearn 执行器（retrieval 等）→ 指路受控生成通道
            #   - 其他未知 → 同样指路，但话说明白
            # 关键：**不再**要 label_column，也不再报"请明确 classification/regression"。
            blockers.append(_unsupported_task_blocker(task_type, where=f"方法{method!r}"))
            suggestions.append(_GENERATED_METRIC_SUGGESTION)
            continue
        if label_column is None:
            blockers.append(f"构建方法{method!r}前data_plan未明确label_column")
            suggestions.append("在data_plan.expected_size.label_column中填写真实标签列")
            continue
        dataset_error = _validate_label_column(label_column, dataset_paths)
        if dataset_error is not None:
            blockers.append(dataset_error)
            suggestions.append("提供唯一CSV数据文件并确认标签列名；模块三不会猜测标签")
            continue
        if family == "logistic_regression" and task_type != "classification":
            blockers.append("逻辑回归执行器只接受明确的classification任务")
            suggestions.append("回归任务请使用线性回归或其他明确的回归方法")
            continue
        if family == "xgboost" and importlib.util.find_spec("xgboost") is None:
            blockers.append("XGBoost未安装，不能在未经批准的情况下修改环境")
            suggestions.append("审查并批准固定版本的xgboost安装计划，或登记已有环境")
            continue
        definition, item = _build_standard_executor(
            request,
            method,
            family,
            task_type,
            label_column,
            run_dir,
            dataset_paths,
        )
        manifest_payload["implementations"][method] = definition.model_dump(mode="python")
        items.append(item)

    if blockers:
        return manifest, None, StageBlocker(
            reason="Implementation Builder无法安全完成实现",
            affected_experiment_ids=request.experiment_plan.primary_experiments,
            blockers=blockers,
            suggested_changes=list(dict.fromkeys(suggestions)),
        )

    if _scientific_manifest_payload(manifest_payload) != _scientific_manifest_payload(
        manifest.model_dump(mode="python")
    ):
        manifest_payload["execution_approved"] = False
        manifest_payload["approval_digest"] = None
        manifest_payload["approved_by"] = None
        manifest_payload["approved_at_utc"] = None
        manifest_payload["approval_type"] = None
        manifest_payload["reviewer_model"] = None
        manifest_payload["review_digest"] = None
        manifest_payload["reviewed_code_digest"] = None
        manifest_payload["reviewed_smoke_digest"] = None
    updated = ImplementationManifest.model_validate(manifest_payload)
    all_ready = all(updated.implementations[name].ready for name in methods)
    metrics_verified = all(
        updated.metrics[name].verified for name in request.experiment_plan.metrics
    )
    review = ImplementationReview(
        run_id=request.run_id,
        task_type=task_type,
        label_column=label_column,
        metric_names=request.experiment_plan.metrics,
        items=items,
        ready_for_approval=all_ready and metrics_verified,
        execution_approved=updated.execution_approved,
        approval_notice=(
            "ready仅表示冒烟测试通过，verified仅表示指标契约通过；"
            "execution_approved只能由两阶段独立LLM审查后的确定性后端授予。"
        ),
    )
    return updated, review, None


def write_generated_implementation(
    proposal: GeneratedImplementationProposal,
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
) -> ImplementationManifest:
    """Write a proposal into one method directory without executing it."""

    if proposal.method not in planned_method_names(request):
        raise ValueError("proposal method is not referenced by the frozen experiment plan")
    lexical_root = run_dir / "implementations"
    lexical_method_dir = lexical_root / slugify(proposal.method)
    if lexical_root.exists() and lexical_root.is_symlink():
        raise ValueError("implementations directory cannot be a symbolic link")
    if lexical_method_dir.exists() and lexical_method_dir.is_symlink():
        raise ValueError("implementation directory cannot be a symbolic link")
    method_dir = safe_join(run_dir, f"implementations/{slugify(proposal.method)}")
    method_dir.mkdir(parents=True, exist_ok=True)
    for existing in method_dir.rglob("*"):
        if existing.is_symlink():
            raise ValueError("implementation directory cannot contain symbolic links")
        if existing.is_file():
            relative = existing.relative_to(method_dir).as_posix()
            if relative not in _ALLOWED_GENERATED_FILES:
                raise ValueError(
                    f"implementation directory contains an unreviewable file: {relative}"
                )
    for name, content in proposal.files.items():
        target = safe_join(method_dir, name)
        if target.exists() and target.is_symlink():
            raise ValueError(f"generated target cannot be a symbolic link: {name}")
        write_text_atomic(target, content)
    payload = manifest.model_dump(mode="python")
    payload["execution_approved"] = False
    payload["approval_digest"] = None
    payload["approved_by"] = None
    payload["approved_at_utc"] = None
    payload["approval_type"] = None
    payload["reviewer_model"] = None
    payload["review_digest"] = None
    payload["reviewed_code_digest"] = None
    payload["reviewed_smoke_digest"] = None
    payload["implementations"][proposal.method] = ImplementationDefinition(
        ready=False,
        planning_aliases=(
            manifest.implementations.get(proposal.method).planning_aliases
            if proposal.method in manifest.implementations
            else []
        ),
        experiment_ids=(
            manifest.implementations.get(proposal.method).experiment_ids
            if proposal.method in manifest.implementations
            else []
        ),
        command=[
            sys.executable,
            relative_to_run(method_dir / "main.py", run_dir),
            "--config",
            "{config_path}",
            "--metrics",
            "{metrics_path}",
        ],
        cwd=".",
        env={},
        required_env=proposal.required_env,
        metrics_path="raw_results/{run_record_id}/metrics.json",
        uses_gpu=proposal.uses_gpu,
        implementation_url=(str(proposal.source_url) if proposal.source_url else None),
        revision=proposal.revision,
        source_kind="GENERATED",
        license=proposal.license,
        dependency_plan=proposal.dependency_plan,
        smoke_test_command=[],
        smoke_test_passed=False,
        notes=proposal.notes,
    ).model_dump(mode="python")
    return ImplementationManifest.model_validate(payload)


#: 任务类型相关的 blocker/suggestion 文案。集中一处，避免三处各写一份、措辞漂移。
#: **不得**再出现「请明确 classification 或 regression」——那是把"模块三没有执行器"
#: 说成"你没给任务类型"，会让 planner 去改一个它已经做对的地方（2026-09-17 事故）。
_TASK_TYPE_SUGGESTION = (
    "在 `data_plan.expected_size.task_type` 里写明任务类型"
    f"（当前支持 {sorted(t.value for t in TaskType)}）；"
    "retrieval/ranking 类任务不需要 label_column，但需要 query/候选/相关性字段。"
)
_GENERATED_METRIC_SUGGESTION = (
    "该任务类型没有模块三内置的标准指标定义。请模块二在 "
    "`experiment_plan.metric_definitions[]` 里登记可执行定义（公式、参数、方向、单位、"
    "必需字段），再由实现子 Agent 经受控生成通道产出确定性计算器，"
    "静态审查 + 小样例验证通过后登记为已验证；**不要**改写成 classification/regression 冒充。"
)


#: 前缀用 ``[data]`` 而不是 ``[experiment]``——路由差异是实质性的：
#:   - ``[data]`` → ``compute_replan_route`` 走 REPLAN_DATA，planner **带着 blocker 原文**
#:     重跑（``_build_replan_data_feedback``），能真正看到"你的 task_type 是什么、
#:     模块三缺什么"。
#:   - ``[experiment]`` → 走 ``replan_only_stage2``：**逐字复用**上一轮 plan，
#:     只有 Layer A 的 gap 会喂给 planner，这条 blocker 的原文到不了它手里。
#: 这两个 blocker 说的都是 ``data_plan.expected_size`` 字段，归 ``[data]`` 既准确又能送达。
_TASK_BLOCKER_PREFIX = "[data]"


def _unsupported_task_blocker(task_type: str, *, where: str) -> str:
    """任务类型**认识**但没有执行器时的诚实 blocker。"""
    return (
        f"{_TASK_BLOCKER_PREFIX} `data_plan.expected_size.task_type` 已识别为 {task_type!r}，"
        f"但模块三没有该任务类型的注册执行器（内置执行器只覆盖 "
        f"{sorted(STANDARD_EXECUTOR_TASK_TYPES)}），因此{where}无法推进。"
        f"这不是「任务类型未明确」——值已经正确传入，缺的是执行通道。"
    )


def _task_type_blocker(unrecognized: str | None, *, where: str) -> str:
    """任务类型缺失或无法识别时的 blocker（带原文回显）。"""
    if unrecognized:
        return (
            f"{_TASK_BLOCKER_PREFIX} `data_plan.expected_size.task_type` 的值 "
            f"{unrecognized!r} 无法识别，因此{where}无法推进（当前支持 "
            f"{sorted(t.value for t in TaskType)}）"
        )
    return (
        f"{_TASK_BLOCKER_PREFIX} `data_plan.expected_size.task_type` 未提供，"
        f"因此{where}无法推进（当前支持 {sorted(t.value for t in TaskType)}）"
    )


def _explicit_task_metadata(
    request: ExperimentModuleInput,
) -> tuple[str | None, str | None, str | None]:
    """``(任务类型, label_column, 无法识别的原始文本)``。

    ⚠️ 历史 bug（2026-09-17 静态审查 F-03）：这里曾把非 classification/regression
    的值**显式归零**，于是模块二明明无损迁移过来的 ``retrieval`` 变成 ``None``，
    下游报出「请明确 task_type 为 classification 或 regression」——把
    **"模块三没有这个执行器"** 误报成 **"你没给任务类型"**，把 planner 引向一个
    它已经做对的地方。

    现在：识别与执行分离。认得出来就原样返回（哪怕没有执行器）；认不出来把原文
    一起返回，让 blocker 能说清楚"你写的是 clustering，模块三不认识"。

    解析走 ``contracts.resolve_task_type``，与 ``review_contracts`` **同一份实现**
    （两份判定漂移会导致「builder 认 retrieval、static review 判 SCHEMA」的撕裂）。
    """
    expected = request.data_plan.expected_size
    task, unrecognized = resolve_task_type(expected)
    raw_label = expected.get("label_column")
    label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else None
    return task, label, unrecognized


def _standard_family(method: str) -> str | None:
    normalized = method.strip().casefold()
    matches = [
        family
        for family, (_canonical, aliases) in _METHOD_ALIASES.items()
        if normalized in {item.casefold() for item in aliases}
    ]
    return matches[0] if len(matches) == 1 else None


#: `recall@10` / `recall_at_10` / `ndcg@5` 这类带 k 的写法。
_METRIC_K_SUFFIX_RE = re.compile(r"(?:@|_at_)(\d+)$")


def _available_metric_names(task_type: str) -> list[str]:
    """该任务类型下可用的**标准**指标名（blocker 文案直接引用，让 planner 照抄）。"""
    names: list[str] = []
    if task_type == TaskType.CLASSIFICATION.value:
        names.extend(sorted(_CLASSIFICATION_METRICS))
    elif task_type == TaskType.REGRESSION.value:
        names.extend(sorted(_REGRESSION_METRICS))
    elif task_type == TaskType.MEMORY_COMPRESSION.value:
        names.extend(sorted(_MEMORY_COMPRESSION_METRICS))
    names.extend(available_metric_names())
    return names


def _split_metric_k(metric: str) -> tuple[str, int | None]:
    """把 ``recall@10`` 拆成 ``("recall_at_k", 10)``。

    检索指标天然带 k，而 planner 会写成 ``recall@10`` / ``recall_at_10`` /
    ``Recall@10`` 各种形态。归一到一个函数名 + 一个参数，注册表才不用为每个 k
    都开一个条目。
    """
    normalized = metric.strip().casefold().replace("-", "_").replace(" ", "")
    match = _METRIC_K_SUFFIX_RE.search(normalized)
    if match:
        return normalized[: match.start()] + "_at_k", int(match.group(1))
    return normalized, None


def _retrieval_metric_definition(metric: str) -> MetricDefinition | None:
    """检索/排序标准指标 → MetricDefinition（``verified=True``）。

    与 ``sklearn`` 那批**同一机制**：公式固定、无歧义、可确定性计算，所以能直接
    声明「用这个实现算」，不需要 LLM 生成代码、也不需要小样本验证。
    """
    base, k = _split_metric_k(metric)
    entry = RETRIEVAL_METRICS.get(base)
    if entry is None:
        return None
    _function, needs_k, description = entry
    parameters: dict[str, Any] = {"k": k if k is not None else 10} if needs_k else {}
    return MetricDefinition(
        verified=True,
        definition_used=f"{description}；{RETRIEVAL_METRIC_MODULE}.{base}",
        implementation=f"{RETRIEVAL_METRIC_MODULE}.{base}",
        parameters=parameters,
        library="Python standard library（模块三内置实现）",
        library_version=None,
        direction="MAXIMIZE",
        unit="score",
        aggregation=(
            "先按 query 计算，再对全部 query 取算术平均；未知/空 ground truth 记 0"
        ),
        seed_aggregation="MEAN",
    )


def _standard_metric_definition(
    metric: str, task_type: str
) -> MetricDefinition | None:
    if task_type == TaskType.MEMORY_COMPRESSION.value:
        details = _MEMORY_COMPRESSION_METRICS.get(metric.casefold())
        if details is None:
            return None
        definition, direction, unit = details
        return MetricDefinition(
            verified=True,
            definition_used=definition,
            implementation=f"memory_compression_qa_v1.{metric.casefold()}",
            parameters={"tokenizer": "unicode_word_regex", "reader": "question_overlap_extractive_v1"},
            library="Python standard library（模块三内置实现）",
            library_version=None,
            direction=direction,
            unit=unit,
            aggregation="compute per example and report arithmetic mean; each run records numerator and denominator audit rows",
            seed_aggregation="MEAN",
        )
    # 检索/排序指标先查：它们与任务类型无关（classification 任务也可能报 recall）
    if task_type not in {"classification", "regression"}:
        return _retrieval_metric_definition(metric)
    registry = (
        _CLASSIFICATION_METRICS
        if task_type == "classification"
        else _REGRESSION_METRICS
    )
    details = registry.get(metric.casefold())
    if details is None:
        # 分类/回归任务里也可能用检索指标（多标签、候选排序等），最后兜一次
        return _retrieval_metric_definition(metric)
    implementation, parameters, direction, unit = details
    if metric.casefold().endswith("_time_seconds"):
        return MetricDefinition(
            verified=True,
            definition_used="使用 time.perf_counter 记录真实训练/推理墙钟时间",
            implementation=f"runtime.{metric.casefold()}",
            parameters={},
            library="Python standard library",
            library_version=None,
            direction=direction,
            unit=unit,
            aggregation="每个固定 seed 的单次运行时间；跨 seed 由模块三计算均值/标准差/极差",
            seed_aggregation="MEAN",
        )
    return MetricDefinition(
        verified=True,
        definition_used=f"sklearn.metrics.{implementation} on the held-out test split",
        implementation=f"sklearn.metrics.{implementation}",
        parameters=parameters,
        library="scikit-learn",
        library_version=_package_version("scikit-learn"),
        direction=direction,
        unit=unit,
        aggregation="metric computed across all held-out test samples",
        seed_aggregation="MEAN",
    )


def _validate_label_column(
    label_column: str, dataset_paths: dict[str, str]
) -> str | None:
    for dataset, raw_path in dataset_paths.items():
        root = Path(raw_path)
        files = [root] if root.is_file() and root.suffix.lower() == ".csv" else sorted(root.rglob("*.csv"))
        if len(files) != 1:
            return f"数据集{dataset!r}必须有且仅有一个CSV文件供通用执行器读取，当前为{len(files)}个"
        with files[0].open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), [])
        if label_column not in header:
            return f"数据集{dataset!r}中找不到明确标签列{label_column!r}"
        if len(header) < 2:
            return f"数据集{dataset!r}除标签外没有可用特征列"
    return None


def _memory_data_files(path: Path) -> list[Path]:
    if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}:
        return [path]
    if path.is_dir():
        return sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in {".json", ".jsonl"})
    return []


def _validate_memory_compression_dataset(dataset_paths: dict[str, str]) -> str | None:
    for dataset, raw_path in dataset_paths.items():
        files = _memory_data_files(Path(raw_path))
        if not files:
            return f"[data] 数据集{dataset!r}没有可执行的 JSON/JSONL 文件"
        groups = (("context", "history", "memory"), ("question", "query", "input"), ("answer", "gold_answer", "answers"))
        saw_object = False
        last_missing: list[str] = []
        # LongBench archives include small metadata JSON files (for example
        # dataset2maxlen.json) before the task JSONL files in lexical order.
        # Those files are not samples and must not make an otherwise valid
        # dataset fail merely because they sort first.
        for path in files:
            try:
                if path.suffix.lower() == ".jsonl":
                    with path.open("r", encoding="utf-8") as handle:
                        first = next(
                            (json.loads(line) for line in handle if line.strip()),
                            None,
                        )
                else:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    rows = (
                        payload if isinstance(payload, list)
                        else payload.get("data") if isinstance(payload, dict)
                        else None
                    )
                    first = rows[0] if isinstance(rows, list) and rows else None
            except (OSError, json.JSONDecodeError, StopIteration):
                continue
            if not isinstance(first, dict):
                continue
            saw_object = True
            last_missing = [
                "/".join(group)
                for group in groups
                if not any(key in first for key in group)
            ]
            if not last_missing:
                break
        else:
            if saw_object:
                return f"[data] 数据集{dataset!r}缺少字段族: {last_missing}"
            return f"[data] 数据集{dataset!r}没有对象形状的样本"
    return None


def _build_memory_compression_executor(
    request: ExperimentModuleInput,
    method: str,
    run_dir: Path,
    dataset_paths: dict[str, str],
) -> tuple[ImplementationDefinition, ImplementationReviewItem]:
    method_dir = safe_join(run_dir, f"implementations/{slugify(method)}")
    method_dir.mkdir(parents=True, exist_ok=True)
    main_path = method_dir / "main.py"
    readme_path = method_dir / "README.md"
    requirements_path = method_dir / "requirements.txt"
    write_text_atomic(main_path, _memory_compression_runner_source(method))
    write_text_atomic(
        readme_path,
        f"# {method}\n\n固定版本 `memory_compression_qa_v1` 执行器。"
        "压缩器只读取问题与上下文，不读取金答案；同一个确定性抽取式 reader "
        "分别评估未压缩和压缩上下文。每次运行写出逐样本 token 计数审计。\n",
    )
    write_text_atomic(requirements_path, "")
    smoke_command = [sys.executable, str(main_path), "--config", "{config_path}", "--metrics", "{metrics_path}"]
    definition = ImplementationDefinition(
        ready=False,
        planning_aliases=[],
        experiment_ids=[],
        command=[sys.executable, relative_to_run(main_path, run_dir), "--config", "{config_path}", "--metrics", "{metrics_path}"],
        cwd=".", env={}, required_env=[],
        metrics_path="raw_results/{run_record_id}/metrics.json",
        uses_gpu=False, implementation_url=None,
        revision="memory-compression-qa-v1",
        source_kind="BUNDLED",
        license="Project-owned deterministic reference implementation",
        dependency_plan=[],
        smoke_test_command=smoke_command,
        smoke_test_passed=False,
        notes=("固定方法语义；20% 方法按 unicode word token 预算压缩；"
               "QA 为不使用金答案的 question-overlap extractive reader，属于可审计代理评估。"),
    )
    item = ImplementationReviewItem(
        method=method, action="BUILD", code_path=relative_to_run(method_dir, run_dir),
        source_kind="BUNDLED", implementation_url=None, revision=definition.revision,
        license=definition.license, dependencies=[],
        command=portable_arguments(definition.command, run_dir=run_dir),
        data_access=[relative_to_run(Path(path), run_dir) for path in dataset_paths.values()],
        resource_requirements={"uses_gpu": False, "gpu_hours_limit": request.resource_constraints.gpu_hours, "memory_gb_limit": request.resource_constraints.memory_gb},
        smoke_test={"passed": False, "command": portable_arguments(smoke_command, run_dir=run_dir), "detail": "等待独立实现审查后的确定性冒烟"},
        ready=False, risks=["qa_token_f1 使用固定抽取式 reader，是下游 QA 保真度代理而非生成式 LLM 准确率"],
    )
    return definition, item


def _memory_compression_runner_source(method: str) -> str:
    return f'''\
import argparse
import hashlib
import json
import math
import re
from pathlib import Path

METHOD = {method!r}
TOKEN = re.compile(r"\\w+(?:[-']\\w+)*", re.UNICODE)
SENTENCE = re.compile(r"(?<=[.!?。！？])\\s+")

def tokens(text):
    return TOKEN.findall(str(text or "").casefold())

def rows_from(path):
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {{".json", ".jsonl"}})
    rows = []
    for file in files:
        if file.suffix.lower() == ".jsonl":
            rows.extend(json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip())
        else:
            payload = json.loads(file.read_text(encoding="utf-8"))
            if isinstance(payload, list): rows.extend(payload)
            elif isinstance(payload, dict) and isinstance(payload.get("data"), list): rows.extend(payload["data"])
    return [row for row in rows if isinstance(row, dict)]

def field(row, names):
    for name in names:
        value = row.get(name)
        if isinstance(value, list) and value: return str(value[0])
        if value is not None and str(value).strip(): return str(value)
    return ""

def chunks(context, size=80):
    sentences = [s.strip() for s in SENTENCE.split(context) if s.strip()]
    if len(sentences) > 1: return sentences
    words = str(context).split()
    return [" ".join(words[i:i+size]) for i in range(0, len(words), size)]

def overlap_score(text, question):
    q = set(tokens(question)); t = set(tokens(text))
    return len(q & t) / max(1, len(q))

def compress(context, question):
    source = tokens(context); budget = max(1, math.ceil(len(source) * 0.20))
    if METHOD == "uncompressed_context": return context
    units = chunks(context)
    if METHOD == "recent_window_20pct": return " ".join(str(context).split()[-budget:])
    scored = []
    for index, unit in enumerate(units):
        score = overlap_score(unit, question)
        if METHOD == "loss_aware_hierarchical_20pct":
            score += 0.05 * len(re.findall(r"\\b(?:\\d+(?:\\.\\d+)?|[A-Z][a-z]+)\\b", unit))
            score += 0.02 / (1 + index)
        scored.append((score, index, unit))
    chosen=[]; used=0
    for _score,index,unit in sorted(scored, key=lambda x: (-x[0], x[1])):
        unit_tokens=tokens(unit)
        if used >= budget: break
        remaining=budget-used
        chosen.append((index, " ".join(unit.split()[:remaining])))
        used += min(len(unit_tokens), remaining)
    return " ".join(text for _index,text in sorted(chosen))

def read_answer(context, question):
    units = chunks(context)
    return max(units, key=lambda unit: overlap_score(unit, question), default="")

def normalize(text):
    return " ".join(tokens(text))

def token_f1(prediction, answer):
    pred=tokens(prediction); gold=tokens(answer)
    if not pred or not gold: return float(pred == gold)
    remaining={{}}
    for token in gold: remaining[token]=remaining.get(token,0)+1
    common=0
    for token in pred:
        if remaining.get(token,0)>0: common+=1; remaining[token]-=1
    if common == 0: return 0.0
    precision=common/len(pred); recall=common/len(gold)
    return 2*precision*recall/(precision+recall)

parser=argparse.ArgumentParser(); parser.add_argument("--config",required=True); parser.add_argument("--metrics",required=True); args=parser.parse_args()
config=json.loads(Path(args.config).read_text(encoding="utf-8"))
dataset_path=Path(config["dataset_path"])
if not dataset_path.is_absolute(): dataset_path=Path.cwd()/dataset_path
rows=rows_from(dataset_path)
if not rows: raise ValueError("memory compression dataset has no JSON rows")
audit=[]
for index,row in enumerate(rows):
    context=field(row,("context","history","memory")); question=field(row,("question","query","input")); answer=field(row,("answer","gold_answer","answers"))
    if not context or not question or not answer: continue
    compressed=compress(context,question); original_n=len(tokens(context)); compressed_n=len(tokens(compressed))
    prediction=read_answer(compressed,question)
    audit.append({{"example_index":index,"example_sha256":hashlib.sha256((question+"\\n"+context).encode("utf-8")).hexdigest(),"original_token_count":original_n,"compressed_token_count":compressed_n,"compression_ratio":original_n/max(1,compressed_n),"qa_token_f1":token_f1(prediction,answer),"answer_exact_match":float(normalize(prediction)==normalize(answer))}})
if not audit: raise ValueError("no rows contain context, question and answer")
metric_names=config["metric_names"]; values={{}}
for name in metric_names:
    key=name.casefold()
    if key not in {{"compression_ratio","qa_token_f1","answer_exact_match","original_token_count","compressed_token_count"}}: raise ValueError(f"unverified metric requested: {{name}}")
    value=sum(float(row[key]) for row in audit)/len(audit)
    unit="times" if key=="compression_ratio" else ("tokens" if key.endswith("token_count") else "score")
    values[name]={{"value":value,"unit":unit,"split":"test"}}
target=Path(args.metrics); target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps({{"metrics":values,"audit":{{"schema_version":1,"method":METHOD,"example_count":len(audit),"rows":audit}}}},ensure_ascii=False),encoding="utf-8")
'''


def _dataset_structure_summary(
    name: str,
    path: Path,
    *,
    task_type: str | None,
    label_column: str | None,
) -> dict[str, Any]:
    """Return schema-only metadata; never include row values."""

    files = [path] if path.is_file() else sorted(
        item for item in path.rglob("*") if item.is_file()
    )
    suffix_counts: dict[str, int] = {}
    csv_summaries: list[dict[str, Any]] = []
    for file in files:
        suffix = file.suffix.lower() or "<none>"
        suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
        if suffix != ".csv":
            continue
        with file.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            columns = next(reader, [])
            row_count = sum(1 for _row in reader)
        csv_summaries.append(
            {
                "path": file.name,
                "columns": columns,
                "row_count": row_count,
                "label_present": bool(label_column and label_column in columns),
            }
        )
    return {
        "dataset": name,
        "file_count": len(files),
        "file_types": suffix_counts,
        "csv_files": csv_summaries,
        "task_type": task_type,
        "label_column": label_column,
        "sample_scale": sum(item["row_count"] for item in csv_summaries),
        "contains_row_values": False,
    }


def _review_existing(
    method: str,
    definition: ImplementationDefinition,
    request: ExperimentModuleInput,
    dataset_paths: dict[str, str],
    run_dir: Path,
) -> ImplementationReviewItem:
    return ImplementationReviewItem(
        method=method,
        action="REUSE",
        code_path=_command_code_path(definition, run_dir),
        source_kind=definition.source_kind,
        implementation_url=(
            str(definition.implementation_url)
            if definition.implementation_url is not None
            else None
        ),
        revision=definition.revision,
        license=definition.license,
        dependencies=definition.dependency_plan,
        command=portable_arguments(definition.command, run_dir=run_dir),
        data_access=[
            relative_to_run(Path(path), run_dir) for path in dataset_paths.values()
        ],
        resource_requirements={
            "uses_gpu": definition.uses_gpu,
            "gpu_hours_limit": request.resource_constraints.gpu_hours,
            "memory_gb_limit": request.resource_constraints.memory_gb,
        },
        smoke_test={
            "passed": definition.smoke_test_passed or definition.ready,
            "command": portable_arguments(definition.smoke_test_command, run_dir=run_dir),
            "basis": "existing manifest ready flag" if not definition.smoke_test_passed else "recorded smoke test",
        },
        ready=definition.ready,
        risks=[] if definition.revision else ["未登记固定revision"],
    )


def _build_standard_executor(
    request: ExperimentModuleInput,
    method: str,
    family: str,
    task_type: str,
    label_column: str,
    run_dir: Path,
    dataset_paths: dict[str, str],
) -> tuple[ImplementationDefinition, ImplementationReviewItem]:
    method_dir = safe_join(run_dir, f"implementations/{slugify(method)}")
    method_dir.mkdir(parents=True, exist_ok=True)
    main_path = method_dir / "main.py"
    readme_path = method_dir / "README.md"
    requirements_path = method_dir / "requirements.txt"
    write_text_atomic(main_path, _sklearn_runner_source(family, task_type, label_column))
    write_text_atomic(
        readme_path,
        _standard_readme(method, family, task_type, label_column),
    )
    dependency_plan = [
        f"pandas=={_package_version('pandas') or '2.2.3'}",
        f"scikit-learn=={_package_version('scikit-learn') or '1.5.2'}",
    ]
    if family == "xgboost":
        dependency_plan.append(f"xgboost=={_package_version('xgboost') or '2.1.1'}")
    write_text_atomic(requirements_path, "\n".join(dependency_plan) + "\n")
    can_test = (
        importlib.util.find_spec("pandas") is not None
        and importlib.util.find_spec("sklearn") is not None
        and (family != "xgboost" or importlib.util.find_spec("xgboost") is not None)
    )
    smoke_command = list(
        [
            sys.executable,
            str(main_path),
            "--config",
            "{config_path}",
            "--metrics",
            "{metrics_path}",
        ]
    )
    smoke_passed = False
    smoke_detail = (
        "依赖已检测；等待根Agent第一阶段独立审查后由确定性后端冒烟"
        if can_test
        else "依赖不在当前环境；仅生成固定版本安装计划，未修改环境"
    )
    definition = ImplementationDefinition(
        ready=smoke_passed,
        planning_aliases=[],
        experiment_ids=[],
        command=[
            sys.executable,
            relative_to_run(main_path, run_dir),
            "--config",
            "{config_path}",
            "--metrics",
            "{metrics_path}",
        ],
        cwd=".",
        env={},
        required_env=[],
        metrics_path="raw_results/{run_record_id}/metrics.json",
        uses_gpu=False,
        implementation_url=None,
        revision="module3-sklearn-runner-v1",
        source_kind="BUNDLED",
        license="BSD-3-Clause dependencies; generated wrapper owned by project",
        dependency_plan=dependency_plan,
        smoke_test_command=smoke_command,
        smoke_test_passed=smoke_passed,
        notes=(
            f"受控通用{family}执行器；task_type={task_type}; "
            f"label_column={label_column}; 只读取config并写入真实metrics文件"
        ),
    )
    item = ImplementationReviewItem(
        method=method,
        action="BUILD",
        code_path=relative_to_run(method_dir, run_dir),
        source_kind="BUNDLED",
        implementation_url=None,
        revision=definition.revision,
        license=definition.license,
        dependencies=dependency_plan,
        command=portable_arguments(definition.command, run_dir=run_dir),
        data_access=[relative_to_run(Path(path), run_dir) for path in dataset_paths.values()],
        resource_requirements={
            "uses_gpu": False,
            "gpu_hours_limit": request.resource_constraints.gpu_hours,
            "memory_gb_limit": request.resource_constraints.memory_gb,
        },
        smoke_test={
            "passed": smoke_passed,
            "command": portable_arguments(smoke_command, run_dir=run_dir),
            "detail": smoke_detail,
        },
        ready=smoke_passed,
        risks=[] if smoke_passed else [smoke_detail],
    )
    return definition, item


def _command_code_path(definition: ImplementationDefinition, run_dir: Path) -> str:
    for argument in definition.command[1:]:
        path = Path(argument)
        if path.is_absolute():
            try:
                return relative_to_run(path, run_dir)
            except ValueError:
                return "external:" + path.name
        candidate = safe_join(run_dir, argument) if "{" not in argument else None
        if candidate is not None and candidate.is_file():
            return relative_to_run(candidate, run_dir)
    return definition.cwd


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _scientific_manifest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for key in (
        "execution_approved",
        "approval_digest",
        "approved_by",
        "approved_at_utc",
        "approval_type",
        "reviewer_model",
        "review_digest",
        "reviewed_code_digest",
        "reviewed_smoke_digest",
    ):
        result.pop(key, None)
    return result


def _standard_readme(
    method: str, family: str, task_type: str, label_column: str
) -> str:
    return (
        f"# {method}\n\n"
        f"受控通用执行器：`{family}`，任务类型 `{task_type}`，标签列 "
        f"`{label_column}`。入口读取配置中的数据路径、seed、参数、切分策略、"
        "预处理和指标名，并只把实际测试集计算结果写入 metrics JSON。\n\n"
        "依赖见 `requirements.txt`；不得把密钥写入本目录。\n"
    )


def _sklearn_runner_source(family: str, task_type: str, label_column: str) -> str:
    return f'''\
import argparse
import json
import math
import time
from pathlib import Path

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


def build_model(seed, parameters):
    family = {family!r}
    task_type = {task_type!r}
    if family == "random_forest":
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        cls = RandomForestClassifier if task_type == "classification" else RandomForestRegressor
        return cls(random_state=seed, **parameters)
    if family == "logistic_regression":
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression(random_state=seed, max_iter=1000, **parameters)
    if family == "svm":
        from sklearn.svm import SVC, SVR
        cls = SVC if task_type == "classification" else SVR
        return cls(**parameters)
    if family == "decision_tree":
        from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
        cls = DecisionTreeClassifier if task_type == "classification" else DecisionTreeRegressor
        return cls(random_state=seed, **parameters)
    if family == "xgboost":
        from xgboost import XGBClassifier, XGBRegressor
        cls = XGBClassifier if task_type == "classification" else XGBRegressor
        return cls(random_state=seed, **parameters)
    raise ValueError("unsupported curated estimator family")


def compute_metrics(names, y_true, y_pred, train_seconds, inference_seconds):
    values = {{}}
    for name in names:
        key = name.casefold()
        if key in {{"accuracy", "top-1_acc"}}:
            value = accuracy_score(y_true, y_pred)
        elif key == "macro_f1":
            value = f1_score(y_true, y_pred, average="macro")
        elif key == "micro_f1":
            value = f1_score(y_true, y_pred, average="micro")
        elif key == "weighted_f1":
            value = f1_score(y_true, y_pred, average="weighted")
        elif key == "mae":
            value = mean_absolute_error(y_true, y_pred)
        elif key == "mse":
            value = mean_squared_error(y_true, y_pred)
        elif key == "rmse":
            value = math.sqrt(mean_squared_error(y_true, y_pred))
        elif key == "r2":
            value = r2_score(y_true, y_pred)
        elif key == "train_time_seconds":
            value = train_seconds
        elif key == "inference_time_seconds":
            value = inference_seconds
        elif key == "total_time_seconds":
            value = train_seconds + inference_seconds
        else:
            raise ValueError(f"unverified metric requested: {{name}}")
        if not math.isfinite(float(value)):
            raise ValueError(f"non-finite metric: {{name}}")
        metric_unit = "seconds" if key.endswith("_time_seconds") else ("score" if key not in {{"mae", "mse", "rmse"}} else None)
        values[name] = {{"value": float(value), "unit": metric_unit, "split": "test"}}
    return values


parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--metrics", required=True)
args = parser.parse_args()
config = json.loads(Path(args.config).read_text(encoding="utf-8"))
dataset_path = Path(config["dataset_path"])
if not dataset_path.is_absolute():
    dataset_path = Path.cwd() / dataset_path
if dataset_path.is_dir():
    csv_files = sorted(dataset_path.rglob("*.csv"))
    if len(csv_files) != 1:
        raise ValueError(f"expected exactly one CSV under dataset directory, got {{len(csv_files)}}")
    dataset_path = csv_files[0]
frame = pd.read_csv(dataset_path)
label_column = {label_column!r}
if label_column not in frame.columns:
    raise ValueError(f"label column not found: {{label_column}}")
X = frame.drop(columns=[label_column])
y = frame[label_column]
numeric = list(X.select_dtypes(include="number").columns)
categorical = [column for column in X.columns if column not in numeric]
preprocessor = ColumnTransformer([
    ("numeric", Pipeline([("imputer", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric),
    ("categorical", Pipeline([("imputer", SimpleImputer(strategy="most_frequent")), ("onehot", OneHotEncoder(handle_unknown="ignore"))]), categorical),
])
split = config["split_strategy"]
if "test" not in split and "test_size" not in split:
    raise ValueError(f"split_strategy does not declare a test fraction: {{split!r}}")
test_size = float(split.get("test", split.get("test_size")))
seed = int(config["seed"])
stratify = y if {task_type!r} == "classification" and y.value_counts().min() >= 2 else None
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=test_size, random_state=seed, stratify=stratify)
model = Pipeline([("preprocess", preprocessor), ("estimator", build_model(seed, dict(config.get("parameters", {{}}))))])
train_start = time.perf_counter()
model.fit(X_train, y_train)
train_seconds = time.perf_counter() - train_start
inference_start = time.perf_counter()
predictions = model.predict(X_test)
inference_seconds = time.perf_counter() - inference_start
payload = {{"metrics": compute_metrics(config["metric_names"], y_test, predictions, train_seconds, inference_seconds)}}
target = Path(args.metrics)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
'''
