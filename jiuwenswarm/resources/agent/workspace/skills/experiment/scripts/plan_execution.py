"""Normalize the planning module's experiment matrix into executable tasks."""

from __future__ import annotations

import json
from collections import defaultdict

from contracts import ExperimentModuleInput
from io_utils import slugify
from runtime_models import (
    ExecutionSpec,
    ExperimentType,
    ImplementationManifest,
    StageBlocker,
)

# Tokens that describe the run rather than the estimator. `seed` belongs to
# execution_config.seeds; a short matrix row such as
# ["EXP-ABL-X", dataset, "random_forest", "seed=42"] would otherwise forward
# seed=42 straight into the estimator constructor and crash the run.
_MATRIX_METADATA_KEYS = {"variant", "seed"}


def planned_method_names(request: ExperimentModuleInput) -> set[str]:
    names: set[str] = set()
    for row in request.experiment_plan.experiment_matrix:
        for token in row[2:]:
            cleaned = token.strip()
            if cleaned and "=" not in cleaned:
                names.add(cleaned)
    return names


def build_execution_specs(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    dataset_paths: dict[str, str],
) -> tuple[list[ExecutionSpec], StageBlocker | None]:
    baseline_names = {item.name for item in request.experiment_plan.baselines}
    matrix_method_names = planned_method_names(request)
    missing_baselines = baseline_names - matrix_method_names
    missing_implementations = matrix_method_names - set(manifest.implementations)
    blockers: list[str] = []
    suggestions: list[str] = []

    if missing_baselines:
        blockers.append(
            "experiment_matrix没有包含以下基线: "
            + ", ".join(sorted(missing_baselines))
        )
        suggestions.append("请模块二把每个计划基线加入对应实验矩阵行")
    if missing_implementations:
        blockers.append(
            "implementation-manifest缺少以下方法实现: "
            + ", ".join(sorted(missing_implementations))
        )
        suggestions.append("实现缺失方法并在manifest.implementations中登记真实命令")

    methods_by_experiment: dict[str, set[str]] = defaultdict(set)
    for row in request.experiment_plan.experiment_matrix:
        methods_by_experiment[row[0]].update(
            token.strip()
            for token in row[2:]
            if token.strip() and "=" not in token
        )
    primary_without_method = [
        experiment_id
        for experiment_id in request.experiment_plan.primary_experiments
        if not (methods_by_experiment[experiment_id] - baseline_names)
    ]
    if primary_without_method:
        blockers.append(
            "以下主实验没有任何非基线方法: "
            + ", ".join(sorted(primary_without_method))
        )
        suggestions.append("请模块二把主方法或其变体加入对应实验矩阵行")

    missing_datasets = {
        row[1]
        for row in request.experiment_plan.experiment_matrix
        if row[1] not in dataset_paths
    }
    if missing_datasets:
        blockers.append("数据尚未准备: " + ", ".join(sorted(missing_datasets)))
        suggestions.append("准备数据集并在manifest.datasets中登记路径")

    if blockers:
        return [], StageBlocker(
            reason="实验矩阵无法标准化为可执行任务",
            affected_experiment_ids=sorted(
                {row[0] for row in request.experiment_plan.experiment_matrix}
            ),
            blockers=blockers,
            suggested_changes=suggestions,
        )

    hypothesis_ids_by_experiment: dict[str, list[str]] = defaultdict(list)
    for coverage in request.method_design.hypothesis_coverage:
        hypothesis_ids_by_experiment[coverage.experiment_ref].append(
            coverage.hypothesis_id
        )

    specs: list[ExecutionSpec] = []
    seen_run_ids: set[str] = set()
    primary_ids = set(request.experiment_plan.primary_experiments)
    for row in request.experiment_plan.experiment_matrix:
        experiment_id, dataset = row[0], row[1]
        methods: list[str] = []
        parameters: dict[str, str] = {}
        for token in row[2:]:
            cleaned = token.strip()
            if not cleaned:
                continue
            if "=" in cleaned:
                key, value = cleaned.split("=", 1)
                if key.strip() and key.strip().casefold() not in _MATRIX_METADATA_KEYS:
                    parameters[key.strip()] = value.strip()
                continue
            if cleaned not in methods:
                methods.append(cleaned)

        for method in methods:
            if experiment_id not in primary_ids:
                experiment_type = ExperimentType.ABLATION
            elif method in baseline_names:
                experiment_type = ExperimentType.BASELINE
            else:
                experiment_type = ExperimentType.PRIMARY
            for seed in request.execution_config.seeds:
                base_id = slugify(f"{experiment_id}-{method}-seed-{seed}")
                run_record_id = base_id
                suffix = 2
                while run_record_id in seen_run_ids:
                    run_record_id = f"{base_id}-{suffix}"
                    suffix += 1
                seen_run_ids.add(run_record_id)
                specs.append(
                    ExecutionSpec(
                        experiment_id=experiment_id,
                        experiment_type=experiment_type,
                        hypothesis_ids=hypothesis_ids_by_experiment[experiment_id],
                        dataset=dataset,
                        dataset_path=dataset_paths[dataset],
                        method=method,
                        metric_names=request.experiment_plan.metrics,
                        seed=seed,
                        run_record_id=run_record_id,
                        parameters=parameters,
                        split_strategy=request.data_plan.split_strategy,
                        preprocessing_pipeline=request.data_plan.preprocessing_pipeline,
                    )
                )

    primary_signatures: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for spec in specs:
        if spec.experiment_type != ExperimentType.PRIMARY.value:
            continue
        parameter_signature = json.dumps(
            spec.parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        primary_signatures[(spec.experiment_id, spec.dataset)].add(
            (spec.method, parameter_signature)
        )
    ambiguous_primary = {
        key: signatures
        for key, signatures in primary_signatures.items()
        if len(signatures) > 1
    }
    if ambiguous_primary:
        descriptions = [
            f"{experiment_id}/{dataset}: "
            + ", ".join(
                f"{method} parameters={parameters}"
                for method, parameters in sorted(signatures)
            )
            for (experiment_id, dataset), signatures in sorted(
                ambiguous_primary.items()
            )
        ]
        return [], StageBlocker(
            reason="主结果方法或参数配置不唯一",
            affected_experiment_ids=sorted(
                {experiment_id for experiment_id, _dataset in ambiguous_primary}
            ),
            blockers=descriptions,
            suggested_changes=[
                "请模块二为每个主实验和数据集明确唯一主方法及主参数配置；"
                "其余变体改为独立实验ID或消融实验"
            ],
        )
    return specs, None
