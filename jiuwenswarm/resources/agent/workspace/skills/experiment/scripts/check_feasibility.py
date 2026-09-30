"""Preflight checks that must pass before any experiment command is launched."""

from __future__ import annotations

import os

from contracts import ExperimentModuleInput
from plan_execution import planned_method_names
from runtime_models import ImplementationManifest, StageBlocker


def check_feasibility(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
) -> tuple[list[str], StageBlocker | None]:
    blockers: list[str] = []
    suggestions: list[str] = []
    warnings: list[str] = []
    affected = sorted(
        {row[0] for row in request.experiment_plan.experiment_matrix}
    )

    estimate = request.experiment_plan.compute_estimate
    budget = request.resource_constraints.gpu_hours
    if estimate > budget:
        blockers.append(
            f"计划预计需要{estimate:g} GPU小时，但资源上限为{budget:g} GPU小时"
        )
        suggestions.append("由模块二缩减实验矩阵，或由用户明确提高GPU小时上限")

    missing_metrics = set(request.experiment_plan.metrics) - set(manifest.metrics)
    if missing_metrics:
        warnings.append(
            "manifest.metrics缺少指标定义: " + ", ".join(sorted(missing_metrics))
        )
    unverified_metrics = [
        name
        for name in request.experiment_plan.metrics
        if name in manifest.metrics and not manifest.metrics[name].verified
    ]
    if unverified_metrics:
        warnings.append(
            "以下指标定义尚未验证: " + ", ".join(sorted(unverified_metrics))
        )
    unsupported_aggregations = [
        name
        for name in request.experiment_plan.metrics
        if name in manifest.metrics
        and manifest.metrics[name].verified
        and manifest.metrics[name].seed_aggregation != "MEAN"
    ]
    if unsupported_aggregations:
        blockers.append(
            "当前执行器只支持跨seed取均值，但以下指标未明确声明该方式: "
            + ", ".join(sorted(unsupported_aggregations))
        )
        suggestions.append(
            "把样本级算法写入aggregation，并把seed_aggregation显式设为MEAN"
        )

    methods = planned_method_names(request)
    missing_methods = methods - set(manifest.implementations)
    if missing_methods:
        warnings.append(
            "manifest.implementations缺少方法: "
            + ", ".join(sorted(missing_methods))
        )
    unready_methods = [
        name
        for name in methods
        if name in manifest.implementations
        and not manifest.implementations[name].ready
    ]
    if unready_methods:
        warnings.append(
            "以下方法实现尚未就绪: " + ", ".join(sorted(unready_methods))
        )

    for name in sorted(methods & set(manifest.implementations)):
        definition = manifest.implementations[name]
        missing_environment = [
            variable for variable in definition.required_env if variable not in os.environ
        ]
        if missing_environment and definition.ready:
            warnings.append(
                f"方法{name}缺少运行环境变量: "
                + ", ".join(missing_environment)
            )

    requires_preprocessing = _requires_preprocessing(
        request.data_plan.preprocessing_pipeline
    )
    datasets_requiring_steps = [
        dataset.name
        for dataset in request.data_plan.datasets
        if dataset.preprocess_required
    ]
    if datasets_requiring_steps and not requires_preprocessing:
        blockers.append(
            "DatasetSpec声明需要预处理，但DataPlan.preprocessing_pipeline是空操作: "
            + ", ".join(datasets_requiring_steps)
        )
        suggestions.append("请模块二把必要预处理步骤写入DataPlan")

    if requires_preprocessing:
        for dataset in request.data_plan.datasets:
            preparation = manifest.dataset_preparations.get(dataset.name)
            if preparation is None:
                blockers.append(f"数据集{dataset.name}缺少预处理实现")
                suggestions.append(
                    "在manifest.dataset_preparations登记与DataPlan一致的真实命令"
                )
                continue
            if not preparation.ready:
                blockers.append(f"数据集{dataset.name}的预处理实现尚未就绪")
                suggestions.append("完成数据预处理冒烟测试后设置ready=true")
            missing_environment = [
                variable
                for variable in preparation.required_env
                if variable not in os.environ
            ]
            if missing_environment:
                blockers.append(
                    f"数据集{dataset.name}预处理缺少环境变量: "
                    + ", ".join(missing_environment)
                )
                suggestions.append("在运行环境中配置变量，不要把密钥值写入manifest")

    for name, extension in manifest.analysis_extensions.items():
        if not extension.ready:
            if extension.required:
                blockers.append(f"必需的领域分析扩展{name}尚未就绪")
                suggestions.append("完成领域分析脚本冒烟测试后设置ready=true")
            else:
                warnings.append(f"可选领域分析扩展{name}未就绪，将跳过")
            continue
        missing_environment = [
            variable for variable in extension.required_env if variable not in os.environ
        ]
        if missing_environment and extension.required:
            blockers.append(
                f"领域分析扩展{name}缺少环境变量: "
                + ", ".join(missing_environment)
            )
            suggestions.append("在运行环境中配置变量，不要把密钥值写入manifest")
        elif missing_environment:
            warnings.append(
                f"可选领域分析扩展{name}缺少环境变量，将跳过: "
                + ", ".join(missing_environment)
            )

    gpu_methods = [
        name
        for name in methods
        if name in manifest.implementations
        and manifest.implementations[name].uses_gpu
    ]
    if gpu_methods and request.resource_constraints.gpu_hours == 0:
        blockers.append(
            "以下实现声明需要GPU，但gpu_hours为0: " + ", ".join(gpu_methods)
        )
        suggestions.append("提供GPU预算或为这些方法登记经过验证的CPU执行命令")

    if not gpu_methods and estimate > 0:
        warnings.append(
            "计划包含GPU耗时估计，但manifest中的实现均未声明uses_gpu=true"
        )

    if blockers:
        return warnings, StageBlocker(
            reason="资源或实现门禁未通过",
            affected_experiment_ids=affected,
            blockers=blockers,
            suggested_changes=_deduplicate(suggestions),
        )
    return warnings, None


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _requires_preprocessing(steps: list[str]) -> bool:
    no_op_values = {"identity", "none", "noop", "no-op", "无需预处理"}
    return any(step.strip().lower() not in no_op_values for step in steps)
