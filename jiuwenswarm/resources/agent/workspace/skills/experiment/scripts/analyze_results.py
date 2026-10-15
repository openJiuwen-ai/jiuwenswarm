"""Build evidence-linked statistics, findings, criteria and hypothesis verdicts."""

from __future__ import annotations

import hashlib
import json
import random
import re
import statistics as statistics_module
from collections import defaultdict
from typing import Iterable

from contracts import (
    AnalysisRecord,
    CriterionEvaluation,
    ExperimentModuleInput,
    ExperimentRun,
    FindingRecord,
    HypothesisEvaluation,
    MetricRecord,
)
from io_utils import slugify
from runtime_models import (
    AggregateRecord,
    ExecutionSpec,
    ImplementationManifest,
    RuntimeRunResult,
)


BOOTSTRAP_RESAMPLES = 2000


def materialize_execution_records(
    specs: list[ExecutionSpec],
    runtime_results: list[RuntimeRunResult],
    manifest: ImplementationManifest,
) -> tuple[list[ExperimentRun], list[MetricRecord]]:
    specs_by_run = {item.run_record_id: item for item in specs}
    experiment_runs: list[ExperimentRun] = []
    metric_records: list[MetricRecord] = []
    for runtime in runtime_results:
        spec = specs_by_run[runtime.run_record_id]
        ids: list[str] = []
        occurrence: dict[tuple[str, str], int] = defaultdict(int)
        for metric_index, metric in enumerate(runtime.metrics, start=1):
            key = (metric.name, metric.split)
            occurrence[key] += 1
            record_id = slugify(
                f"MR-{runtime.run_record_id}-{metric_index}-{metric.name}-"
                f"{metric.split}-{occurrence[key]}"
            )
            ids.append(record_id)
            unit = metric.unit or manifest.metrics[metric.name].unit
            metric_records.append(
                MetricRecord(
                    record_id=record_id,
                    experiment_id=spec.experiment_id,
                    dataset=spec.dataset,
                    method=spec.method,
                    metric=metric.name,
                    value=metric.value,
                    unit=unit,
                    seed=spec.seed,
                    split=metric.split,
                    parameters=spec.parameters,
                )
            )
        experiment_runs.append(
            ExperimentRun(
                run_record_id=runtime.run_record_id,
                experiment_id=spec.experiment_id,
                status="SUCCESS" if runtime.success else "FAILED",
                dataset=spec.dataset,
                method=spec.method,
                command=runtime.command,
                config_path=runtime.config_path,
                log_path=runtime.log_path,
                metric_record_ids=ids,
                error=runtime.error,
            )
        )
    return experiment_runs, metric_records


def aggregate_metrics(
    metric_records: list[MetricRecord],
    manifest: ImplementationManifest,
) -> list[AggregateRecord]:
    test_records = [item for item in metric_records if item.split == "test"]
    groups: dict[tuple[str, str, str, str, str], list[MetricRecord]] = defaultdict(list)
    for item in test_records:
        signature = json.dumps(
            item.parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        groups[
            (item.experiment_id, item.dataset, item.method, item.metric, signature)
        ].append(item)

    aggregates: list[AggregateRecord] = []
    for (
        experiment_id,
        dataset,
        method,
        metric,
        _signature,
    ), records in sorted(groups.items()):
        values = [item.value for item in records]
        low, high = _bootstrap_interval(
            values,
            f"{experiment_id}|{dataset}|{method}|{metric}|{_signature}",
        )
        definition = manifest.metrics[metric]
        if definition.seed_aggregation != "MEAN":
            raise ValueError(
                f"metric {metric} does not explicitly allow MEAN seed aggregation"
            )
        aggregates.append(
            AggregateRecord(
                experiment_id=experiment_id,
                dataset=dataset,
                method=method,
                metric=metric,
                parameters=records[0].parameters,
                direction=definition.direction,
                unit=definition.unit or _first_unit(records),
                count=len(values),
                mean=statistics_module.fmean(values),
                std=statistics_module.stdev(values) if len(values) > 1 else 0.0,
                minimum=min(values),
                maximum=max(values),
                ci95_low=low,
                ci95_high=high,
                source_experiment_ids=[experiment_id],
                source_metric_record_ids=[item.record_id for item in records],
            )
        )
    return aggregates


def build_analysis_records(
    aggregates: list[AggregateRecord],
    request: ExperimentModuleInput,
) -> list[AnalysisRecord]:
    grouped: dict[tuple[str, str, str, str], list[AggregateRecord]] = defaultdict(list)
    for aggregate in aggregates:
        signature = json.dumps(
            aggregate.parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        grouped[
            (
                aggregate.experiment_id,
                aggregate.dataset,
                aggregate.metric,
                signature,
            )
        ].append(aggregate)

    records: list[AnalysisRecord] = []
    for analysis_index, ((experiment_id, dataset, metric, _signature), items) in enumerate(
        sorted(grouped.items()),
        start=1,
    ):
        repeated = any(item.count > 1 for item in items)
        method_name = (
            "seed_level_bootstrap_comparison"
            if repeated
            else "single_run_descriptive_comparison"
        )
        assumptions = ["同一方法不同seed的运行使用相同数据切分"] if repeated else []
        limitations = [] if repeated else ["每个方法只有一次有效运行，无法估计跨seed波动"]
        records.append(
            AnalysisRecord(
                analysis_id=slugify(
                    f"AN-{analysis_index}-{experiment_id}-{dataset}-{metric}-{_signature}"
                ),
                method_name=method_name,
                purpose=f"比较实验{experiment_id}在{dataset}上各方法的{metric}",
                source_experiment_ids=[experiment_id],
                source_metric_record_ids=[
                    record_id
                    for item in items
                    for record_id in item.source_metric_record_ids
                ],
                parameters={
                    "confidence": 0.95,
                    "bootstrap_resamples": BOOTSTRAP_RESAMPLES if repeated else 0,
                    "aggregation": "mean_across_successful_test_runs",
                    "domain": request.domain.domain_name,
                    "variant_parameters": items[0].parameters,
                },
                assumptions=assumptions,
                results={
                    "groups": [
                        {
                            "method": item.method,
                            "count": item.count,
                            "mean": item.mean,
                            "std": item.std,
                            "ci95": [item.ci95_low, item.ci95_high],
                            "minimum": item.minimum,
                            "maximum": item.maximum,
                        }
                        for item in items
                    ]
                },
                summary=_analysis_summary(experiment_id, dataset, metric, items),
                limitations=limitations,
            )
        )
        if len(items) >= 2:
            ordered = sorted(
                items,
                key=lambda item: item.mean,
                reverse=items[0].direction == "MAXIMIZE",
            )
            best, comparator = ordered[0], ordered[1]
            favorable_delta = (
                best.mean - comparator.mean
                if best.direction == "MAXIMIZE"
                else comparator.mean - best.mean
            )
            relative_delta = (
                favorable_delta / abs(comparator.mean) * 100
                if comparator.mean != 0
                else None
            )
            records.append(
                AnalysisRecord(
                    analysis_id=slugify(
                        f"AN-CONTRAST-{experiment_id}-{dataset}-{metric}-{_signature}"
                    ),
                    method_name="direction_aware_best_method_contrast",
                    purpose=(
                        f"量化实验{experiment_id}在{dataset}上{metric}最优方法"
                        "相对次优方法的差距"
                    ),
                    source_experiment_ids=[experiment_id],
                    source_metric_record_ids=[
                        record_id
                        for item in (best, comparator)
                        for record_id in item.source_metric_record_ids
                    ],
                    parameters={
                        "direction": best.direction,
                        "comparison": "best_mean_vs_second_best_mean",
                    },
                    assumptions=["比较基于相同实验、数据集、指标和参数签名"],
                    results={
                        "best_method": best.method,
                        "comparator_method": comparator.method,
                        "best_mean": best.mean,
                        "comparator_mean": comparator.mean,
                        "favorable_absolute_delta": favorable_delta,
                        "relative_delta_percent": relative_delta,
                    },
                    summary=(
                        f"{best.method}为当前均值最优方法，相对{comparator.method}的"
                        f"有利绝对差值为{favorable_delta:.6g}。"
                    ),
                    limitations=[
                        "该差值是跨成功seed均值之差；配对seed证据应结合候选配对图解释"
                    ],
                )
            )
        repeated_items = [item for item in items if item.count > 1]
        if repeated_items:
            most_stable = min(repeated_items, key=lambda item: item.std)
            least_stable = max(repeated_items, key=lambda item: item.std)
            records.append(
                AnalysisRecord(
                    analysis_id=slugify(
                        f"AN-STABILITY-{experiment_id}-{dataset}-{metric}-{_signature}"
                    ),
                    method_name="seed_repeatability_audit",
                    purpose=(
                        f"审计实验{experiment_id}在{dataset}上{metric}的跨seed稳定性"
                    ),
                    source_experiment_ids=[experiment_id],
                    source_metric_record_ids=[
                        record_id
                        for item in repeated_items
                        for record_id in item.source_metric_record_ids
                    ],
                    parameters={
                        "dispersion": ["standard_deviation", "observed_range", "ci95_width"],
                        "aggregation": "successful_test_runs_only",
                    },
                    assumptions=["seed是本次重复运行的比较单位"],
                    results={
                        "most_stable_method": most_stable.method,
                        "most_stable_std": most_stable.std,
                        "least_stable_method": least_stable.method,
                        "least_stable_std": least_stable.std,
                        "groups": [
                            {
                                "method": item.method,
                                "std": item.std,
                                "observed_range": item.maximum - item.minimum,
                                "ci95_width": item.ci95_high - item.ci95_low,
                            }
                            for item in repeated_items
                        ],
                    },
                    summary=(
                        f"{most_stable.method}在当前成功seed中的标准差最小"
                        f"（{most_stable.std:.6g}）；{least_stable.method}最大"
                        f"（{least_stable.std:.6g}）。"
                    ),
                    limitations=[
                        "seed数量较少时稳定性排序本身也存在不确定性"
                    ],
                )
            )

    cross_dataset: dict[tuple[str, str, str], list[AggregateRecord]] = defaultdict(list)
    for item in aggregates:
        cross_dataset[(item.experiment_id, item.method, item.metric)].append(item)
    for (experiment_id, method, metric), items in sorted(cross_dataset.items()):
        datasets = {item.dataset for item in items}
        if len(datasets) < 2:
            continue
        dataset_means = {item.dataset: item.mean for item in items}
        records.append(
            AnalysisRecord(
                analysis_id=slugify(
                    f"AN-GENERALIZATION-{experiment_id}-{method}-{metric}"
                ),
                method_name="cross_dataset_consistency",
                purpose=f"评估{method}在多个数据集上的{metric}一致性",
                source_experiment_ids=[experiment_id],
                source_metric_record_ids=[
                    record_id
                    for item in items
                    for record_id in item.source_metric_record_ids
                ],
                parameters={"aggregation": "mean_by_dataset"},
                assumptions=["各数据集使用同一定义的指标和方向"],
                results={
                    "dataset_means": dataset_means,
                    "between_dataset_range": max(dataset_means.values())
                    - min(dataset_means.values()),
                },
                summary=(
                    f"{method}在{len(datasets)}个数据集上的{metric}均值范围为"
                    f"{min(dataset_means.values()):.6g}至{max(dataset_means.values()):.6g}。"
                ),
                limitations=["数据集间难度不同，绝对均值不可直接解释为领域迁移因果效应"],
            )
        )
    return records


def build_findings(
    aggregates: list[AggregateRecord],
    request: ExperimentModuleInput,
) -> tuple[list[str], list[FindingRecord]]:
    grouped: dict[tuple[str, str, str, str], list[AggregateRecord]] = defaultdict(list)
    for aggregate in aggregates:
        signature = json.dumps(
            aggregate.parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        grouped[
            (
                aggregate.experiment_id,
                aggregate.dataset,
                aggregate.metric,
                signature,
            )
        ].append(aggregate)
    hypothesis_by_experiment: dict[str, set[str]] = defaultdict(set)
    for coverage in request.method_design.hypothesis_coverage:
        hypothesis_by_experiment[coverage.experiment_ref].add(coverage.hypothesis_id)

    findings: list[FindingRecord] = []
    for index, ((experiment_id, dataset, metric, _signature), items) in enumerate(
        sorted(grouped.items()), start=1
    ):
        direction = items[0].direction
        best = (
            max(items, key=lambda item: item.mean)
            if direction == "MAXIMIZE"
            else min(items, key=lambda item: item.mean)
        )
        direction_word = "最高" if direction == "MAXIMIZE" else "最低"
        parameter_phrase = _parameter_phrase(best.parameters)
        statement = (
            f"实验{experiment_id}在{dataset}上，{best.method}{parameter_phrase}的"
            f"{metric}均值为"
            f"{best.mean:.6g}，在已成功运行的方法中{direction_word}。"
        )
        experiment_ids = [experiment_id]
        related_hypotheses = sorted(
            {
                hypothesis_id
                for experiment_id in experiment_ids
                for hypothesis_id in hypothesis_by_experiment[experiment_id]
            }
        )
        findings.append(
            FindingRecord(
                finding_id=f"F{index}",
                statement=statement,
                evidence_experiment_ids=experiment_ids,
                metric_names=[metric],
                related_hypothesis_ids=related_hypotheses,
            )
        )
    return [item.statement for item in findings], findings


def build_head_statistics(
    aggregates: list[AggregateRecord],
    request: ExperimentModuleInput,
) -> dict[str, float]:
    first_dataset = request.experiment_plan.datasets[0].name
    first_primary_experiment = request.experiment_plan.primary_experiments[0]
    baseline_names = {item.name for item in request.experiment_plan.baselines}
    statistics: dict[str, float] = {}
    for metric in request.experiment_plan.metrics:
        candidates = [
            item
            for item in aggregates
            if item.dataset == first_dataset
            and item.experiment_id == first_primary_experiment
            and item.metric == metric
            and item.method not in baseline_names
        ]
        if len(candidates) != 1:
            variants = [
                {
                    "method": item.method,
                    "parameters": item.parameters,
                }
                for item in candidates
            ]
            raise ValueError(
                f"指标{metric}需要唯一主方法/参数结果，当前候选={variants}"
            )
        statistics[metric] = candidates[0].mean
    return statistics


def evaluate_criteria(
    aggregates: list[AggregateRecord],
    request: ExperimentModuleInput,
) -> list[CriterionEvaluation]:
    evaluations: list[CriterionEvaluation] = []
    for index, criterion in enumerate(request.experiment_plan.success_criteria, start=1):
        experiment_id = _resolve_experiment_id(criterion, request)
        metric = _resolve_metric(criterion, request.experiment_plan.metrics)
        evaluation = _evaluate_criterion(
            criterion,
            experiment_id,
            metric,
            aggregates,
            request,
        )
        evaluations.append(
            CriterionEvaluation(
                criterion_id=f"C{index}",
                source_criterion=criterion,
                experiment_id=experiment_id,
                metric=metric,
                actual_value=evaluation[0],
                passed=evaluation[1],
                reason=evaluation[2],
            )
        )
    return evaluations


def evaluate_hypotheses(
    criteria: list[CriterionEvaluation],
    request: ExperimentModuleInput,
) -> list[HypothesisEvaluation]:
    experiment_ids_by_hypothesis: dict[str, set[str]] = defaultdict(set)
    for coverage in request.method_design.hypothesis_coverage:
        experiment_ids_by_hypothesis[coverage.hypothesis_id].add(
            coverage.experiment_ref
        )

    evaluations: list[HypothesisEvaluation] = []
    for hypothesis_id, experiment_ids in sorted(experiment_ids_by_hypothesis.items()):
        relevant = [item for item in criteria if item.experiment_id in experiment_ids]
        if not relevant or any(item.passed is None for item in relevant):
            verdict = "INCONCLUSIVE"
            reason = "至少一条关联成功标准无法无歧义计算"
        elif any(item.passed is False for item in relevant):
            verdict = "NOT_SUPPORTED"
            reason = "至少一条可判定的成功标准未达到"
        elif all(item.passed is True for item in relevant):
            verdict = "SUPPORTED"
            reason = "所有可判定且关联的成功标准均达到"
        else:
            verdict = "INCONCLUSIVE"
            reason = "缺少无歧义且可计算的关联成功标准"
        evaluations.append(
            HypothesisEvaluation(
                hypothesis_id=hypothesis_id,
                verdict=verdict,
                evidence_experiment_ids=sorted(experiment_ids),
                reason=reason,
            )
        )
    return evaluations


def _evaluate_criterion(
    criterion: str,
    experiment_id: str | None,
    metric: str | None,
    aggregates: list[AggregateRecord],
    request: ExperimentModuleInput,
) -> tuple[float | None, bool | None, str]:
    if experiment_id is None:
        return None, None, "成功标准未明确对应实验，且存在多个主实验"
    if metric is None:
        return None, None, "成功标准未包含无歧义的计划指标名称"
    if (
        "提升" in criterion
        or "improv" in criterion.lower()
        or ("比" in criterion and ("高" in criterion or "低" in criterion))
    ):
        return _evaluate_improvement(criterion, experiment_id, metric, aggregates, request)

    comparison = _parse_comparison(criterion, metric)
    if comparison is None:
        return None, None, "当前规则无法无歧义解析比较符和阈值"
    operator, threshold = comparison
    candidates = _aggregates_for_experiment(
        aggregates, experiment_id, metric, request
    )
    baseline_names = {item.name for item in request.experiment_plan.baselines}
    primary = [item for item in candidates if item.method not in baseline_names]
    if len(primary) != 1:
        return None, None, "成功标准未指定方法，且该实验存在多个非基线方法"
    actual = primary[0].mean
    return actual, _compare(actual, operator, threshold), f"使用{primary[0].method}的均值进行判断"


def _evaluate_improvement(
    criterion: str,
    experiment_id: str,
    metric: str,
    aggregates: list[AggregateRecord],
    request: ExperimentModuleInput,
) -> tuple[float | None, bool | None, str]:
    comparison = _parse_comparison(criterion, metric, allow_text_between=True)
    if comparison is None:
        return None, None, "提升标准缺少可解析的比较符或阈值"
    operator, threshold = comparison
    candidates = _aggregates_for_experiment(
        aggregates, experiment_id, metric, request
    )
    baseline_names = {item.name for item in request.experiment_plan.baselines}
    baseline_name = _resolve_baseline(criterion, sorted(baseline_names))
    if baseline_name is None:
        return None, None, "提升标准无法唯一确定所比较的基线"
    primary = [item for item in candidates if item.method not in baseline_names]
    baselines = [item for item in candidates if item.method == baseline_name]
    if len(primary) != 1 or len(baselines) != 1:
        return None, None, "提升标准需要唯一主方法和唯一基线，当前映射不唯一"

    lower = criterion.lower()
    raw_delta = primary[0].mean - baselines[0].mean
    if "低" in criterion and "高" not in criterion:
        raw_delta = -raw_delta
    if "百分点" in criterion or "个点" in criterion or "pp" in lower:
        actual = raw_delta * 100
        return (
            actual,
            _compare(actual, operator, threshold),
            f"相对基线{baseline_name}按百分点差值计算",
        )
    if "相对" in criterion or "relative" in lower:
        if baselines[0].mean == 0:
            return None, None, "基线为0，无法计算相对提升"
        actual = raw_delta / abs(baselines[0].mean) * 100
        return (
            actual,
            _compare(actual, operator, threshold),
            f"相对基线{baseline_name}按相对百分比计算",
        )
    if "%" in criterion:
        return None, None, "“提升百分比”未说明相对提升还是百分点，拒绝静默猜测"
    actual = raw_delta
    return (
        actual,
        _compare(actual, operator, threshold),
        f"相对基线{baseline_name}按指标绝对差值计算",
    )


def _aggregates_for_experiment(
    aggregates: list[AggregateRecord],
    experiment_id: str,
    metric: str,
    request: ExperimentModuleInput,
) -> list[AggregateRecord]:
    datasets = {
        row[1]
        for row in request.experiment_plan.experiment_matrix
        if row[0] == experiment_id
    }
    methods = {
        token
        for row in request.experiment_plan.experiment_matrix
        if row[0] == experiment_id
        for token in row[2:]
        if token and "=" not in token
    }
    return [
        item
        for item in aggregates
        if item.experiment_id == experiment_id
        and item.metric == metric
        and item.dataset in datasets
        and item.method in methods
        and experiment_id in item.source_experiment_ids
    ]


def _parse_comparison(
    criterion: str,
    metric: str,
    *,
    allow_text_between: bool = False,
) -> tuple[str, float] | None:
    normalized = criterion.replace("≥", ">=").replace("≤", "<=")
    separator = ".*?" if allow_text_between else r"\s*"
    match = re.search(
        rf"{re.escape(metric)}{separator}(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*(%)?",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match and allow_text_between:
        # Module 2 currently writes the metric after the threshold, e.g.
        # “比 baseline_A 高 ≥ 3 个点 (top-1_acc)”.  Accept it only when
        # exactly one comparison exists and the already-resolved metric is
        # explicitly present in this criterion.
        occurrences = re.findall(
            r"(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*(%)?",
            normalized,
        )
        if metric.casefold() in normalized.casefold() and len(occurrences) == 1:
            operator, value, percent = occurrences[0]
            match_payload = (operator, float(value), percent)
        else:
            match_payload = None
    else:
        match_payload = (
            (match.group(1), float(match.group(2)), match.group(3))
            if match
            else None
        )
    if match_payload is None:
        return None
    operator, value, percent = match_payload
    if percent and not ("提升" in criterion or "improv" in criterion.lower()):
        value /= 100
    return operator, value


def _compare(actual: float, operator: str, threshold: float) -> bool:
    return {
        ">=": actual >= threshold,
        "<=": actual <= threshold,
        ">": actual > threshold,
        "<": actual < threshold,
    }[operator]


def _resolve_metric(criterion: str, metrics: list[str]) -> str | None:
    aliases = {
        "macro_f1_mean": "macro_f1",
        "macro_f1_std": "macro_f1",
        "macro_f1_range": "macro_f1",
        "accuracy_mean": "accuracy",
        "accuracy_std": "accuracy",
        "accuracy_range": "accuracy",
        "train_time_mean": "train_time_seconds",
        "inference_time_mean": "inference_time_seconds",
        "total_time_mean": "total_time_seconds",
    }
    matches = []
    lowered = criterion.lower()
    for metric in metrics:
        if metric.lower() in lowered:
            matches.append(metric)
            continue
        for alias, base in aliases.items():
            if base == metric.lower() and alias in lowered:
                matches.append(metric)
                break
    return matches[0] if len(matches) == 1 else None


def _resolve_experiment_id(
    criterion: str,
    request: ExperimentModuleInput,
) -> str | None:
    all_ids = {row[0] for row in request.experiment_plan.experiment_matrix}
    matches = [item for item in all_ids if item.lower() in criterion.lower()]
    if len(matches) == 1:
        return matches[0]
    primary = list(dict.fromkeys(request.experiment_plan.primary_experiments))
    return primary[0] if len(primary) == 1 else None


def _resolve_baseline(criterion: str, baselines: list[str]) -> str | None:
    lowered = criterion.casefold()
    matches = [name for name in baselines if name.casefold() in lowered]
    if len(matches) == 1:
        return matches[0]
    if not matches and len(baselines) == 1:
        return baselines[0]
    return None


def _analysis_summary(
    experiment_id: str,
    dataset: str,
    metric: str,
    items: list[AggregateRecord],
) -> str:
    direction = items[0].direction
    best = (
        max(items, key=lambda item: item.mean)
        if direction == "MAXIMIZE"
        else min(items, key=lambda item: item.mean)
    )
    return (
        f"实验{experiment_id}在{dataset}上的{metric}比较中，"
        f"{best.method}{_parameter_phrase(best.parameters)}的均值为"
        f"{best.mean:.6g}；"
        "区间与波动仅由成功运行记录计算。"
    )


def _parameter_phrase(parameters: dict[str, str]) -> str:
    if not parameters:
        return ""
    rendered = ", ".join(
        f"{key}={value}" for key, value in sorted(parameters.items())
    )
    return f"（{rendered}）"


def _bootstrap_interval(values: list[float], label: str) -> tuple[float, float]:
    if len(values) == 1:
        return values[0], values[0]
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    generator = random.Random(int.from_bytes(digest[:8], "big"))
    means = sorted(
        statistics_module.fmean(generator.choice(values) for _ in values)
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    low_index = int(0.025 * (len(means) - 1))
    high_index = int(0.975 * (len(means) - 1))
    return means[low_index], means[high_index]


def _first_unit(records: Iterable[MetricRecord]) -> str | None:
    return next((item.unit for item in records if item.unit is not None), None)
