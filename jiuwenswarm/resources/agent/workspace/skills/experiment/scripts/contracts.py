"""Pydantic contracts for the Experiment module public boundary."""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


def _validate_relative_path(value: str) -> str:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if (
        windows.drive
        or windows.is_absolute()
        or posix.is_absolute()
        or ".." in posix.parts
    ):
        raise ValueError("path must be relative and cannot contain '..'")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class TaskType(str, Enum):
    """实验任务类型。

    历史问题（2026-09-17 静态审查 F-03）：模块三只承认 classification/regression，
    其余值在 ``implementation_builder._explicit_task_metadata`` 里被**显式归零**，
    随后报出「请明确 task_type 为 classification 或 regression」——而模块二其实
    已经无损迁移了 ``retrieval``（见 ``projection_report`` 的「无损迁移」记录）。
    真正缺的不是"任务类型没给"，而是"给了但模块三没有对应执行器"。

    所以这里把词表显式化：**识别**与**能否执行**分开表达，别再让前者背后者的锅。
    """

    CLASSIFICATION = "classification"
    REGRESSION = "regression"
    RETRIEVAL = "retrieval"
    RANKING = "ranking"
    QA = "qa"
    GENERATION = "generation"
    MEMORY_COMPRESSION = "memory_compression"


#: 走模块三内置标准执行器（sklearn 一族）的任务类型。
STANDARD_EXECUTOR_TASK_TYPES: frozenset[str] = frozenset({
    TaskType.CLASSIFICATION.value,
    TaskType.REGRESSION.value,
    TaskType.MEMORY_COMPRESSION.value,
})

#: 每种任务类型的「必需字段族」——每族里命中任意一个字段名即算满足。
#: 分类/回归要 label；检索/排序要 query、候选、相关性（或 ground truth）。
TASK_REQUIRED_FIELD_FAMILIES: dict[str, tuple[tuple[str, ...], ...]] = {
    TaskType.CLASSIFICATION.value: (("label_column", "label", "target", "class"),),
    TaskType.REGRESSION.value: (("label_column", "label", "target"),),
    TaskType.RETRIEVAL.value: (
        ("query_id", "query", "qid", "question"),
        ("doc_id", "candidate_id", "passage_id", "corpus_id", "memory_id"),
        ("relevance", "gold_id", "answer_session_ids", "evidence", "has_answer"),
    ),
    TaskType.RANKING.value: (
        ("query_id", "query", "qid"),
        ("doc_id", "candidate_id", "passage_id"),
        ("relevance", "grade", "label"),
    ),
    TaskType.QA.value: (("question", "query"), ("answer", "gold_answer")),
    TaskType.GENERATION.value: (("prompt", "input"), ("reference", "target", "output")),
    TaskType.MEMORY_COMPRESSION.value: (
        ("context", "history", "memory"),
        ("question", "query"),
        ("answer", "gold_answer", "answers"),
    ),
}


def resolve_task_type(expected_size: Any) -> tuple[str | None, str | None]:
    """从 ``data_plan.expected_size`` 解析任务类型。

    返回 ``(归一化任务类型或 None, 无法识别的原始文本或 None)``。

    **单一真值**：``implementation_builder`` 与 ``review_contracts`` 必须都调这里——
    历史上两处各写一份判定，改一处就会出现「builder 认 retrieval、static review
    判 SCHEMA」的撕裂。

    只做归一化（大小写 / 空白 / 连字符），**不做语义推断**：认不出来就原样回显，
    让 blocker 文案能说清楚"你写的是什么"。
    """
    if not isinstance(expected_size, dict):
        return None, None
    raw = expected_size.get("task_type")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "classification": TaskType.CLASSIFICATION.value,
        "分类": TaskType.CLASSIFICATION.value,
        "regression": TaskType.REGRESSION.value,
        "回归": TaskType.REGRESSION.value,
        "retrieval": TaskType.RETRIEVAL.value,
        "检索": TaskType.RETRIEVAL.value,
        "ranking": TaskType.RANKING.value,
        "排序": TaskType.RANKING.value,
        "qa": TaskType.QA.value,
        "question_answering": TaskType.QA.value,
        "问答": TaskType.QA.value,
        "generation": TaskType.GENERATION.value,
        "生成": TaskType.GENERATION.value,
        "memory_compression": TaskType.MEMORY_COMPRESSION.value,
        "agent_memory_compression": TaskType.MEMORY_COMPRESSION.value,
        "记忆压缩": TaskType.MEMORY_COMPRESSION.value,
    }
    resolved = aliases.get(normalized)
    if resolved is None:
        return None, raw.strip()
    return resolved, None


class ModuleStatus(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    REPLAN = "REPLAN"
    FAILED = "FAILED"


class RunStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class HypothesisVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class VisualizationDataLevel(str, Enum):
    RAW = "RAW"
    AGGREGATED = "AGGREGATED"


class VisualizationKind(str, Enum):
    FIGURE = "FIGURE"
    TABLE = "TABLE"


class VisualizationPriority(str, Enum):
    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"
    OPTIONAL = "OPTIONAL"


class Component(ContractModel):
    name: str = Field(min_length=1)
    function: str = Field(min_length=1)
    input_schema: str = Field(min_length=1)
    output_schema: str = Field(min_length=1)
    novelty_degree: str = Field(pattern="^(novel|adapted|standard)$")


class InnovationPoint(ContractModel):
    claim: str = Field(min_length=1)
    evidence_metric: str = Field(min_length=1)
    experiment_ref: str = Field(min_length=1)


class HypothesisCoverage(ContractModel):
    hypothesis_id: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    experiment_ref: str = Field(min_length=1)


class MethodDesign(ContractModel):
    research_goal: str = Field(min_length=1)
    core_mechanism: str = Field(min_length=1)
    framework: str = Field(min_length=1)
    components: list[Component] = Field(min_length=1)
    technical_route: str = Field(min_length=1)
    algorithm_reference: list[str] = Field(default_factory=list)
    innovation_points: list[InnovationPoint] = Field(min_length=1)
    hypothesis_coverage: list[HypothesisCoverage] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    implementable: bool


class DatasetSpec(ContractModel):
    name: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    scale_estimate: str = Field(min_length=1)
    license: str | None = None
    readiness: str = Field(pattern="^(available|download|apply)$")
    preprocess_required: list[str] = Field(default_factory=list)
    # 这些字段是 retrieval/ranking/QA 数据能否真正执行的关键契约。它们是
    # dataset-specific 的，不能只放进 data_plan.expected_size：不同数据集通常使用
    # 不同的 JSON 键名。全部可选以保持旧版 classification 请求向后兼容；缺失时
    # 实现阶段必须 REPLAN，不能从数据集名称猜。
    task_type: str | None = None
    query_field: str | None = None
    query_id_field: str | None = None
    candidate_field: str | None = None
    candidate_id_field: str | None = None
    corpus_field: str | None = None
    relevance_field: str | None = None
    relevance_granularity: str | None = None
    ground_truth_field: str | None = None
    question_field: str | None = None
    answer_field: str | None = None
    usage_constraint: str | None = None


class BaselineSpec(ContractModel):
    name: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)


class Ablation(ContractModel):
    component: str = Field(min_length=1)
    removed_by: str = Field(min_length=1)
    expected_impact: str | None = None


class MetricDefinitionSpec(ContractModel):
    """模块二给出的**可执行指标定义**（提案，不是已验证事实）。

    历史事故（2026-09-17 静态审查 F-02）：规划 LLM 自造了一个平铺的
    ``metric_definitions`` 键（含 ``formula`` / ``ground_truth_mapping`` 等），模块二放行落盘，
    但投影层按 ``ExperimentPlan`` 白名单静默裁掉了它——模块三只收到字符串指标名，
    于是自定义指标（``key_recall`` 这类）永远"未验证"，实现阶段必然 blocker。

    ⚠️ **这个模型上不存在 ``verified`` 字段**，这是结构性的：模块二没有资格声明
    指标已验证。``verified=true`` 只能由模块三后端在受控生成 + 静态审查 + 小样例
    验证之后写入 ``manifest.metrics``。
    """

    metric_name: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    implementation: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    direction: str | None = None
    unit: str | None = None
    sample_unit: str | None = None
    within_run_aggregation: str | None = None
    seed_aggregation: str | None = None
    required_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_planner_flat_shape(cls, value: Any) -> Any:
        """吸收规划 LLM 实际写出的**平铺形状**。

        真实产物形如::

            {"metric_name": "key_recall",
             "formula": "key_recall = |{i ∈ top_k : ...}| / |ground_truth.key_items|",
             "numerator": "检索返回的 top-k 条目中命中 ground-truth 的条数",
             "denominator": "ground-truth 关键信息条目总数",
             "top_k": 10,
             "ground_truth_mapping": "LongMemEval：从 answer_session_ids 提取…"}

        **只做字段搬运，不做推导**：``formula`` → ``definition``；其余标量键整体
        折进 ``parameters``；``numerator``/``denominator``/``ground_truth_mapping``
        逐字追加到 ``definition`` 尾部作为可读从句（丢掉它们等于丢掉指标语义）。
        """
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        formula = payload.pop("formula", None)
        if not payload.get("definition") and isinstance(formula, str) and formula.strip():
            payload["definition"] = formula.strip()
        clauses: list[str] = []
        for key, label in (
            ("numerator", "分子"),
            ("denominator", "分母"),
            ("ground_truth_mapping", "真值映射"),
        ):
            raw = payload.pop(key, None)
            if isinstance(raw, str) and raw.strip():
                clauses.append(f"{label}：{raw.strip()}")
        if clauses:
            payload["definition"] = (
                str(payload.get("definition") or "").rstrip() + "；" + "；".join(clauses)
            ).strip("；")
        # 其余未知键：标量折进 parameters，嵌套结构转成文本补进 definition
        known = set(cls.model_fields)
        extra_params = dict(payload.get("parameters") or {})
        for key in [k for k in payload if k not in known]:
            raw = payload.pop(key)
            if isinstance(raw, (str, int, float, bool)) and str(raw).strip():
                extra_params.setdefault(key, raw)
            elif raw not in (None, [], {}):
                payload["definition"] = (
                    str(payload.get("definition") or "") + f"；{key}：{raw}"
                )
        if extra_params:
            payload["parameters"] = extra_params
        return payload


class ExperimentPlan(ContractModel):
    objectives: list[str] = Field(min_length=1)
    datasets: list[DatasetSpec] = Field(min_length=1)
    baselines: list[BaselineSpec] = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)
    #: 指标的可执行定义（可选）。**默认空 → 旧请求照常校验**，向后兼容。
    #: 没有它，自定义指标（检索类等）在模块三只能落到"硬编码表里没有"→ blocker。
    metric_definitions: list[MetricDefinitionSpec] = Field(default_factory=list)
    primary_experiments: list[str] = Field(min_length=1)
    experiment_matrix: list[list[str]] = Field(min_length=1)
    ablation_plan: list[Ablation] = Field(default_factory=list)
    expected_results: list[str] = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    compute_estimate: float = Field(ge=0)

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> "ExperimentPlan":
        _require_unique(
            [item.name for item in self.datasets],
            "experiment_plan.datasets names",
        )
        _require_unique(
            [item.name for item in self.baselines],
            "experiment_plan.baselines names",
        )
        _require_unique(self.metrics, "experiment_plan.metrics")
        _require_unique(
            self.primary_experiments,
            "experiment_plan.primary_experiments",
        )
        unknown_baseline_metrics = {
            item.metric_name
            for item in self.baselines
            if item.metric_name not in self.metrics
        }
        if unknown_baseline_metrics:
            raise ValueError(
                "baseline metric_name values must be planned metrics: "
                f"{sorted(unknown_baseline_metrics)}"
            )
        _require_unique(
            [item.metric_name for item in self.metric_definitions],
            "experiment_plan.metric_definitions names",
        )
        unknown_defined_metrics = {
            item.metric_name
            for item in self.metric_definitions
            if item.metric_name not in self.metrics
        }
        if unknown_defined_metrics:
            raise ValueError(
                "metric_definitions[].metric_name must be planned metrics: "
                f"{sorted(unknown_defined_metrics)}"
            )
        return self


class DataPlan(ContractModel):
    datasets: list[DatasetSpec] = Field(min_length=1)
    split_strategy: dict[str, Any]
    preprocessing_pipeline: list[str] = Field(min_length=1)
    expected_size: dict[str, Any]

    @model_validator(mode="after")
    def split_has_seed(self) -> "DataPlan":
        if "seed" not in self.split_strategy:
            raise ValueError("data_plan.split_strategy.seed is required")
        _require_unique(
            [item.name for item in self.datasets],
            "data_plan.datasets names",
        )
        return self


class Domain(ContractModel):
    domain_name: str = Field(min_length=1)


class ResourceConstraints(ContractModel):
    gpu_type: str | None = None
    gpu_hours: int = Field(ge=0)
    memory_gb: int = Field(gt=0)
    budget: float | None = Field(default=None, ge=0)
    time_budget_days: int = Field(gt=0)


class ExecutionConfig(ContractModel):
    run_dir: str = Field(min_length=1)
    seeds: list[int] = Field(min_length=1)
    max_retries: int = Field(ge=0)
    timeout_seconds: int | None = Field(default=None, gt=0)
    max_parallel_runs: int = Field(default=4, ge=1, le=64)
    max_parallel_gpu_runs: int = Field(default=1, ge=1, le=8)
    monitor_interval_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    environment_mode: str = Field(
        default="CURRENT",
        pattern="^(CURRENT|VENV)$",
    )
    allow_dependency_install: bool = False
    min_figure_candidates: int = Field(default=4, ge=1, le=48)
    max_figure_candidates: int = Field(default=8, ge=1, le=96)
    dry_run: bool = False

    _run_dir_is_relative = field_validator("run_dir")(_validate_relative_path)

    @model_validator(mode="after")
    def seeds_are_unique(self) -> "ExecutionConfig":
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("execution_config.seeds values must be unique")
        if self.max_parallel_gpu_runs > self.max_parallel_runs:
            raise ValueError(
                "max_parallel_gpu_runs cannot exceed max_parallel_runs"
            )
        if self.min_figure_candidates > self.max_figure_candidates:
            raise ValueError(
                "min_figure_candidates cannot exceed max_figure_candidates"
            )
        return self


class ExperimentModuleInput(ContractModel):
    schema_version: str = Field(pattern=r"^2\.\d+\.\d+$")
    run_id: str = Field(min_length=1)
    method_design: MethodDesign
    experiment_plan: ExperimentPlan
    data_plan: DataPlan
    domain: Domain
    resource_constraints: ResourceConstraints
    execution_config: ExecutionConfig

    @model_validator(mode="after")
    def validate_cross_references(self) -> "ExperimentModuleInput":
        if not self.method_design.implementable:
            raise ValueError("method_design.implementable must be true")

        matrix_rows = self.experiment_plan.experiment_matrix
        malformed_rows = [index for index, row in enumerate(matrix_rows) if len(row) < 4]
        if malformed_rows:
            raise ValueError(
                "experiment_matrix rows must follow "
                "[experiment_id, dataset_name, method_or_baseline, variant, ...]; "
                f"invalid row indexes: {malformed_rows}"
            )
        blank_required_cells = [
            index
            for index, row in enumerate(matrix_rows)
            if any(not item.strip() for item in row[:4])
        ]
        if blank_required_cells:
            raise ValueError(
                "experiment_matrix first four values cannot be blank; "
                f"invalid row indexes: {blank_required_cells}"
            )
        duplicate_parameter_rows: list[int] = []
        for index, row in enumerate(matrix_rows):
            parameter_names = [
                token.split("=", 1)[0].strip()
                for token in row[2:]
                if "=" in token
            ]
            if any(not name for name in parameter_names) or len(
                parameter_names
            ) != len(set(parameter_names)):
                duplicate_parameter_rows.append(index)
        if duplicate_parameter_rows:
            raise ValueError(
                "experiment_matrix parameters require unique non-empty keys; "
                f"invalid row indexes: {duplicate_parameter_rows}"
            )

        matrix_ids = {row[0] for row in matrix_rows}
        missing_primary = set(self.experiment_plan.primary_experiments) - matrix_ids
        if missing_primary:
            raise ValueError(
                "primary experiments missing from experiment_matrix: "
                f"{sorted(missing_primary)}"
            )
        planned_ids = set(self.experiment_plan.primary_experiments) | matrix_ids
        referenced_ids = {
            point.experiment_ref for point in self.method_design.innovation_points
        }
        referenced_ids.update(
            item.experiment_ref for item in self.method_design.hypothesis_coverage
        )
        if missing := referenced_ids - planned_ids:
            raise ValueError(f"unplanned experiment references: {sorted(missing)}")

        plan_dataset_specs = {
            item.name: item.model_dump(mode="json")
            for item in self.experiment_plan.datasets
        }
        data_dataset_specs = {
            item.name: item.model_dump(mode="json")
            for item in self.data_plan.datasets
        }
        plan_datasets = set(plan_dataset_specs)
        if plan_dataset_specs != data_dataset_specs:
            raise ValueError("experiment_plan.datasets and data_plan.datasets differ")

        unknown_matrix_datasets = {row[1] for row in matrix_rows} - plan_datasets
        if unknown_matrix_datasets:
            raise ValueError(
                f"experiment_matrix uses unknown datasets: {sorted(unknown_matrix_datasets)}"
            )

        metric_names = set(self.experiment_plan.metrics)
        invalid_metrics = {
            point.evidence_metric
            for point in self.method_design.innovation_points
            if point.evidence_metric not in metric_names
        }
        if invalid_metrics:
            raise ValueError(f"innovation metrics not in plan: {sorted(invalid_metrics)}")
        return self


def _require_unique(values: list[str], field_name: str) -> None:
    duplicates = sorted(
        {value for value in values if values.count(value) > 1}
    )
    if duplicates:
        raise ValueError(f"{field_name} must be unique: {duplicates}")


class MetricRecord(ContractModel):
    record_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    method: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    value: float
    unit: str | None = None
    seed: int | None = None
    split: str = Field(pattern="^(train|val|test)$")
    parameters: dict[str, str] = Field(default_factory=dict)


class ExperimentRun(ContractModel):
    run_record_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    status: RunStatus
    dataset: str = Field(min_length=1)
    method: str = Field(min_length=1)
    command: str = Field(min_length=1)
    config_path: str = Field(min_length=1)
    log_path: str = Field(min_length=1)
    metric_record_ids: list[str] = Field(default_factory=list)
    error: str | None = None

    _paths_are_relative = field_validator("config_path", "log_path")(
        _validate_relative_path
    )

    @model_validator(mode="after")
    def validate_status_evidence(self) -> "ExperimentRun":
        if self.status == RunStatus.SUCCESS.value:
            if self.error is not None or not self.metric_record_ids:
                raise ValueError(
                    "SUCCESS run requires metric_record_ids and forbids error"
                )
        elif self.status == RunStatus.FAILED.value:
            if not self.error or self.metric_record_ids:
                raise ValueError(
                    "FAILED run requires error and forbids metric_record_ids"
                )
        elif self.status == RunStatus.SKIPPED.value and not self.error:
            raise ValueError("SKIPPED run requires a reason in error")
        return self


class FindingRecord(ContractModel):
    finding_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_experiment_ids: list[str] = Field(min_length=1)
    metric_names: list[str] = Field(min_length=1)
    related_hypothesis_ids: list[str] = Field(default_factory=list)


class HypothesisEvaluation(ContractModel):
    hypothesis_id: str = Field(min_length=1)
    verdict: HypothesisVerdict
    evidence_experiment_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class MetricImplementation(ContractModel):
    name: str = Field(min_length=1)
    definition_used: str = Field(min_length=1)
    implementation: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    library: str | None = None
    library_version: str | None = None
    direction: str = Field(pattern="^(MAXIMIZE|MINIMIZE)$")
    unit: str | None = None
    aggregation: str = Field(min_length=1)
    seed_aggregation: str = Field(pattern="^MEAN$")


class BaselineImplementation(ContractModel):
    name: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    implementation_url: HttpUrl | None = None
    revision: str | None = None
    entry_command: str = Field(min_length=1)
    implementation_notes: str = Field(min_length=1)


class CriterionEvaluation(ContractModel):
    criterion_id: str = Field(min_length=1)
    source_criterion: str = Field(min_length=1)
    experiment_id: str | None = None
    metric: str | None = None
    actual_value: float | None = None
    passed: bool | None
    reason: str = Field(min_length=1)


class AnalysisRecord(ContractModel):
    analysis_id: str = Field(min_length=1)
    method_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    source_experiment_ids: list[str] = Field(min_length=1)
    source_metric_record_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    results: dict[str, Any] = Field(min_length=1)
    summary: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)


class VisualizationDataAsset(ContractModel):
    data_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    format: str = Field(min_length=1)
    data_level: VisualizationDataLevel
    column_schema: dict[str, str] = Field(min_length=1)
    units: dict[str, str | None] = Field(default_factory=dict)
    row_count: int = Field(ge=0)
    source_experiment_ids: list[str] = Field(min_length=1)
    source_metric_record_ids: list[str] = Field(default_factory=list)
    aggregation: str | None = None
    description: str = Field(min_length=1)

    _path_is_relative = field_validator("path")(_validate_relative_path)


class VisualizationCandidate(ContractModel):
    candidate_id: str = Field(min_length=1)
    kind: VisualizationKind
    visualization_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    domain_rationale: str = Field(min_length=1)
    source_data_ids: list[str] = Field(min_length=1)
    source_experiment_ids: list[str] = Field(min_length=1)
    analysis_ids: list[str] = Field(min_length=1)
    related_finding_ids: list[str] = Field(min_length=1)
    design_spec: dict[str, Any] = Field(min_length=1)
    priority: VisualizationPriority
    recommended_for: list[str] = Field(min_length=1)
    preview_path: str | None = None
    editable_spec_path: str | None = None
    caption_draft: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("preview_path", "editable_spec_path")
    @classmethod
    def optional_paths_are_relative(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_path(value)


class TableArtifact(ContractModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    source_experiment_ids: list[str] = Field(min_length=1)

    _path_is_relative = field_validator("path")(_validate_relative_path)


class FigureArtifact(ContractModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    caption: str = Field(min_length=1)
    source_experiment_ids: list[str] = Field(min_length=1)
    # Semantic transfer contract for Module 4.  A pixel file and hash establish
    # provenance, but not what the marks mean.  Older extension outputs remain
    # loadable; missing fields make a figure non-reusable rather than causing a
    # whole completed experiment to fail validation.
    displayed_metrics: list[str] = Field(default_factory=list)
    caption_assertions: list[str] = Field(default_factory=list)
    source_data_ids: list[str] = Field(default_factory=list)
    evidence_role: str | None = None
    display_label_map: dict[str, str] = Field(default_factory=dict)

    _path_is_relative = field_validator("path")(_validate_relative_path)


class ExperimentResults(ContractModel):
    key_findings: list[str] = Field(min_length=1)
    tables: dict[str, str]
    figures: dict[str, str]
    statistics: dict[str, float] = Field(min_length=1)
    finding_records: list[FindingRecord] = Field(min_length=1)
    metric_records: list[MetricRecord] = Field(min_length=1)
    experiment_runs: list[ExperimentRun] = Field(min_length=1)
    hypothesis_evaluations: list[HypothesisEvaluation] = Field(min_length=1)
    metric_implementations: list[MetricImplementation] = Field(min_length=1)
    baseline_implementations: list[BaselineImplementation] = Field(min_length=1)
    criterion_evaluations: list[CriterionEvaluation] = Field(min_length=1)
    analysis_records: list[AnalysisRecord] = Field(min_length=1)
    visualization_data: list[VisualizationDataAsset] = Field(min_length=1)
    visualization_candidates: list[VisualizationCandidate] = Field(min_length=1)
    writing_brief_path: str = Field(min_length=1)
    table_artifacts: list[TableArtifact] = Field(default_factory=list)
    figure_artifacts: list[FigureArtifact] = Field(default_factory=list)

    _writing_brief_path_is_relative = field_validator("writing_brief_path")(
        _validate_relative_path
    )

    @model_validator(mode="after")
    def validate_evidence_links(self) -> "ExperimentResults":
        finding_statements = [item.statement for item in self.finding_records]
        if set(self.key_findings) != set(finding_statements):
            raise ValueError(
                "key_findings must match finding_records statements exactly"
            )

        metric_record_ids = [item.record_id for item in self.metric_records]
        if len(metric_record_ids) != len(set(metric_record_ids)):
            raise ValueError("metric_records.record_id values must be unique")

        run_record_ids = [item.run_record_id for item in self.experiment_runs]
        if len(run_record_ids) != len(set(run_record_ids)):
            raise ValueError("experiment_runs.run_record_id values must be unique")

        known_experiment_ids = {
            item.experiment_id for item in self.experiment_runs
        }
        referenced_experiment_ids = {
            item.experiment_id for item in self.metric_records
        }
        referenced_experiment_ids.update(
            experiment_id
            for finding in self.finding_records
            for experiment_id in finding.evidence_experiment_ids
        )
        referenced_experiment_ids.update(
            experiment_id
            for evaluation in self.hypothesis_evaluations
            for experiment_id in evaluation.evidence_experiment_ids
        )
        referenced_experiment_ids.update(
            item.experiment_id
            for item in self.criterion_evaluations
            if item.experiment_id is not None
        )
        referenced_experiment_ids.update(
            experiment_id
            for analysis in self.analysis_records
            for experiment_id in analysis.source_experiment_ids
        )
        referenced_experiment_ids.update(
            experiment_id
            for asset in self.visualization_data
            for experiment_id in asset.source_experiment_ids
        )
        referenced_experiment_ids.update(
            experiment_id
            for candidate in self.visualization_candidates
            for experiment_id in candidate.source_experiment_ids
        )
        referenced_experiment_ids.update(
            experiment_id
            for artifact in self.table_artifacts
            for experiment_id in artifact.source_experiment_ids
        )
        referenced_experiment_ids.update(
            experiment_id
            for artifact in self.figure_artifacts
            for experiment_id in artifact.source_experiment_ids
        )
        if unknown_experiment_refs := (
            referenced_experiment_ids - known_experiment_ids
        ):
            raise ValueError(
                "results reference unknown experiments: "
                f"{sorted(unknown_experiment_refs)}"
            )

        known_metric_record_ids = set(metric_record_ids)
        run_metric_refs = {
            record_id
            for run in self.experiment_runs
            for record_id in run.metric_record_ids
        }
        if run_metric_refs != known_metric_record_ids:
            raise ValueError(
                "experiment_runs and metric_records must reference exactly the "
                "same metric record IDs"
            )

        successful_experiment_ids = {
            item.experiment_id
            for item in self.experiment_runs
            if item.status == RunStatus.SUCCESS.value
        }
        evidence_experiment_ids = {
            experiment_id
            for finding in self.finding_records
            for experiment_id in finding.evidence_experiment_ids
        }
        evidence_experiment_ids.update(
            experiment_id
            for analysis in self.analysis_records
            for experiment_id in analysis.source_experiment_ids
        )
        if invalid_evidence := evidence_experiment_ids - successful_experiment_ids:
            raise ValueError(
                "findings or analyses cite experiments without a successful run: "
                f"{sorted(invalid_evidence)}"
            )

        metric_names = [item.name for item in self.metric_implementations]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("metric_implementations.name values must be unique")
        if set(self.statistics) != set(metric_names):
            raise ValueError(
                "statistics keys must match metric_implementations names"
            )

        baseline_names = [item.name for item in self.baseline_implementations]
        if len(baseline_names) != len(set(baseline_names)):
            raise ValueError("baseline_implementations.name values must be unique")

        finding_ids = [item.finding_id for item in self.finding_records]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding_records.finding_id values must be unique")

        analysis_ids = [item.analysis_id for item in self.analysis_records]
        if len(analysis_ids) != len(set(analysis_ids)):
            raise ValueError("analysis_records.analysis_id values must be unique")

        data_ids = [item.data_id for item in self.visualization_data]
        if len(data_ids) != len(set(data_ids)):
            raise ValueError("visualization_data.data_id values must be unique")
        if not any(
            item.data_level == VisualizationDataLevel.RAW.value
            for item in self.visualization_data
        ):
            raise ValueError("visualization_data must include at least one RAW asset")

        candidate_ids = [
            item.candidate_id for item in self.visualization_candidates
        ]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError(
                "visualization_candidates.candidate_id values must be unique"
            )

        unknown_analysis_metric_refs = {
            record_id
            for analysis in self.analysis_records
            for record_id in analysis.source_metric_record_ids
            if record_id not in known_metric_record_ids
        }
        unknown_data_metric_refs = {
            record_id
            for asset in self.visualization_data
            for record_id in asset.source_metric_record_ids
            if record_id not in known_metric_record_ids
        }
        if unknown_analysis_metric_refs or unknown_data_metric_refs:
            raise ValueError(
                "analysis or visualization data references unknown metric records: "
                f"{sorted(unknown_analysis_metric_refs | unknown_data_metric_refs)}"
            )

        known_analysis_ids = set(analysis_ids)
        known_data_ids = set(data_ids)
        known_finding_ids = set(finding_ids)
        unknown_candidate_data_refs = {
            data_id
            for candidate in self.visualization_candidates
            for data_id in candidate.source_data_ids
            if data_id not in known_data_ids
        }
        unknown_candidate_analysis_refs = {
            analysis_id
            for candidate in self.visualization_candidates
            for analysis_id in candidate.analysis_ids
            if analysis_id not in known_analysis_ids
        }
        unknown_candidate_finding_refs = {
            finding_id
            for candidate in self.visualization_candidates
            for finding_id in candidate.related_finding_ids
            if finding_id not in known_finding_ids
        }
        if (
            unknown_candidate_data_refs
            or unknown_candidate_analysis_refs
            or unknown_candidate_finding_refs
        ):
            raise ValueError(
                "visualization candidates contain unknown references: "
                f"data={sorted(unknown_candidate_data_refs)}, "
                f"analysis={sorted(unknown_candidate_analysis_refs)}, "
                f"findings={sorted(unknown_candidate_finding_refs)}"
            )

        table_names = {item.name for item in self.table_artifacts}
        if set(self.tables) != table_names:
            raise ValueError("tables keys must match table_artifacts names")

        figure_names = {item.name for item in self.figure_artifacts}
        if set(self.figures) != figure_names:
            raise ValueError("figures keys must match figure_artifacts names")
        return self


class ResourceUsage(ContractModel):
    wall_time_seconds: float = Field(ge=0)
    gpu_hours: float = Field(ge=0)
    cpu_hours: float = Field(ge=0)
    peak_memory_gb: float | None = Field(default=None, ge=0)
    llm_prompt_tokens: int = Field(ge=0)
    llm_completion_tokens: int = Field(ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    retry_count: int = Field(ge=0)


class Reproducibility(ContractModel):
    environment_path: str = Field(min_length=1)
    environment_deployment_path: str = Field(min_length=1)
    entry_command: str = Field(min_length=1)
    seeds: list[int] = Field(min_length=1)
    results_csv_path: str = Field(min_length=1)
    raw_results_dir: str = Field(min_length=1)
    logs_dir: str = Field(min_length=1)
    execution_monitor_path: str = Field(min_length=1)
    execution_events_path: str = Field(min_length=1)

    _paths_are_relative = field_validator(
        "environment_path",
        "environment_deployment_path",
        "results_csv_path",
        "raw_results_dir",
        "logs_dir",
        "execution_monitor_path",
        "execution_events_path",
    )(_validate_relative_path)


class PlanningFeedback(ContractModel):
    reason: str = Field(min_length=1)
    affected_experiment_ids: list[str] = Field(min_length=1)
    blockers: list[str] = Field(min_length=1)
    suggested_changes: list[str] = Field(min_length=1)

    @field_validator("blockers")
    @classmethod
    def blockers_have_categories(cls, values: list[str]) -> list[str]:
        """Make every planning blocker routable without discarding its text."""

        return [categorize_blocker(value) for value in values]


#: 已经带路由前缀的 blocker 原样保留。这里的类别集合必须与模块二
#: ``load_inputs._FEEDBACK_CATEGORIES``（8 类别）一致——历史上这里少了
#: ``experiment``/``budget``，于是模块三显式写好的 ``[experiment] …`` 不会被识别，
#: 会被下面的关键词规则重新分类：含"实现"字样的消融 blocker 因此被判成 ``[method]``，
#: 把 REPLAN 路由去**重跑 method-designer**（而修法其实在 experiment_plan 侧）。
_BLOCKER_PREFIX = re.compile(
    r"^\[(data|compute|baseline|method|metric|schema|experiment|budget)\]\s*",
    flags=re.IGNORECASE,
)


#: ``experiment_plan`` 自身字段的问题单独判，**用字段路径锚定**、且**先于**关键词规则。
#: 两个理由：
#:   1. 修法在 planner 侧（experiment_plan 是它产出的）。判成 ``[method]`` 会让 REPLAN
#:      去重跑 method-designer，而那条路由**根本改不到 experiment_plan** —— 2026-09-17
#:      前科：``removed_by`` 散文 blocker 因含"实现"被判 ``[method]``，method-designer
#:      结构化输出重试耗尽 → planning error → 顶层 ``aborted_planning_replan``。
#:   2. 关键词是**子串**匹配，会被 blocker 里引用的字段值抢走：同一批 blocker 里
#:      ``…removed_by='…metric_variant 从 full 改为 basic…'`` 就因含 "metric" 被判成了
#:      ``[metric]``。字段路径（``experiment_plan.`` / ``ablation``）不会出现在其他类别
#:      的 blocker 里，锚定它才稳。
#: 锚定判定表：``(标记元组, 类别)``，**按顺序**先命中先返回。
#:
#: 为什么按「字段路径」锚定而不是靠关键词：关键词是**子串**匹配，会被 blocker 里
#: 引用的字段**值**抢走（真实事故：``…removed_by='…metric_variant 从 full 改为 basic…'``
#: 因含 "metric" 被判成 ``[metric]``）。
#:
#: 为什么 ``data_plan.`` 单独一类：``expected_size.task_type`` 这类问题修法在
#: **planner** 手里，``[data]`` 会经 ``compute_replan_route`` 走 REPLAN_DATA
#: （planner 带反馈重跑），正是能改到 ``data_plan`` 的那条路。历史事故：含
#: ``task_type`` 的 blocker 被关键词判成 ``[method]`` → REPLAN 去重跑
#: method-designer，而 method-design 的产物里根本没有 ``data_plan``。
_ANCHORED_CATEGORIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("experiment_plan.", "ablation", "消融"), "experiment"),
    (("data_plan.",), "data"),
)


def categorize_blocker(value: str) -> str:
    """Attach the stable Module-2 routing category required by the handoff."""

    stripped = value.strip()
    if _BLOCKER_PREFIX.match(stripped):
        return stripped
    lowered = stripped.lower()
    for markers, category in _ANCHORED_CATEGORIES:
        if any(marker in lowered for marker in markers):
            return f"[{category}] {stripped}"
    rules = (
        (
            "data",
            (
                "dataset",
                "数据",
                "标签列",
                "label column",
                "预处理",
                "download",
                "下载",
                "fingerprint",
                "指纹",
            ),
        ),
        (
            "compute",
            (
                "gpu",
                "cpu",
                "显存",
                "内存",
                "算力",
                "预算",
                "资源",
                "超时",
                "timeout",
                "依赖",
                "环境变量",
            ),
        ),
        ("baseline", ("baseline", "基线")),
        (
            "metric",
            (
                "metric",
                "指标",
                "成功标准",
                "阈值",
                "aggregation",
                "聚合",
                "f1",
                "accuracy",
            ),
        ),
        (
            "method",
            (
                "method",
                "方法",
                "实现",
                "implementation",
                "模型",
            ),
        ),
    )
    category = next(
        (
            name
            for name, markers in rules
            if any(marker in lowered for marker in markers)
        ),
        "schema",
    )
    return f"[{category}] {stripped}"


class ExperimentModuleOutput(ContractModel):
    schema_version: str = Field(pattern=r"^2\.\d+\.\d+$")
    run_id: str = Field(min_length=1)
    status: ModuleStatus
    experiment_results: ExperimentResults | None = None
    resource_usage: ResourceUsage
    reproducibility: Reproducibility | None = None
    planning_feedback: PlanningFeedback | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_payload(self) -> "ExperimentModuleOutput":
        if self.status in {ModuleStatus.PASS.value, ModuleStatus.PARTIAL.value}:
            if self.experiment_results is None or self.reproducibility is None:
                raise ValueError(
                    "PASS/PARTIAL requires experiment_results and reproducibility"
                )
            if self.planning_feedback is not None or self.errors:
                raise ValueError(
                    "PASS/PARTIAL forbids planning_feedback and errors"
                )
            successful = [
                item
                for item in self.experiment_results.experiment_runs
                if item.status == RunStatus.SUCCESS.value
            ]
            if not successful:
                raise ValueError("PASS/PARTIAL requires at least one successful run")
            if (
                self.status == ModuleStatus.PASS.value
                and len(successful) != len(self.experiment_results.experiment_runs)
            ):
                raise ValueError("PASS forbids failed or skipped experiment runs")
        elif self.status == ModuleStatus.REPLAN.value:
            if self.planning_feedback is None:
                raise ValueError("REPLAN requires planning_feedback")
            if self.experiment_results is not None or self.reproducibility is not None:
                raise ValueError(
                    "REPLAN forbids experiment_results and reproducibility"
                )
            if self.errors:
                raise ValueError("REPLAN uses planning_feedback instead of errors")
        elif self.status == ModuleStatus.FAILED.value:
            if not self.errors:
                raise ValueError("FAILED requires at least one error")
            if (
                self.experiment_results is not None
                or self.reproducibility is not None
                or self.planning_feedback is not None
            ):
                raise ValueError(
                    "FAILED forbids experiment_results, reproducibility and planning_feedback"
                )
        return self
