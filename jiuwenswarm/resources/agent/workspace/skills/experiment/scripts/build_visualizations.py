"""Export plotting data and build editable, evidence-linked visual candidates."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from pathlib import Path

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(tempfile.gettempdir()) / "jiuwenswarm-module3-matplotlib"),
)
import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

from contracts import (
    AnalysisRecord,
    FigureArtifact,
    FindingRecord,
    MetricRecord,
    TableArtifact,
    VisualizationCandidate,
    VisualizationDataAsset,
)
from io_utils import (
    relative_to_run,
    slugify,
    write_csv_atomic,
    write_json_atomic,
    write_text_atomic,
)
from runtime_models import AggregateRecord
from visualization_catalog import audit_figure_exports, build_catalog_candidates
from figure_style import (
    compact_label,
    display_label,
    method_color,
    polish_axis,
    publication_font_stack,
    publication_rc,
)


logging.getLogger("fontTools.subset").setLevel(logging.WARNING)


def _metric_priority(metric: str) -> tuple[int, str]:
    normalized = metric.casefold().replace("-", "_")
    exact = {
        "macro_f1": 0,
        "accuracy": 1,
        "f1": 2,
        "roc_auc": 3,
        "pr_auc": 4,
        "recall_at_k": 5,
        "ndcg_at_k": 6,
        "mrr": 7,
        "total_time_seconds": 20,
        "inference_time_seconds": 21,
        "train_time_seconds": 22,
    }
    if normalized in exact:
        return exact[normalized], normalized
    if any(token in normalized for token in ("time", "latency", "memory", "cost")):
        return 25, normalized
    if any(token in normalized for token in ("micro_f1", "weighted_f1")):
        return 12, normalized
    return 10, normalized


def _metric_family(metric: str) -> str:
    normalized = metric.casefold().replace("-", "_")
    if any(token in normalized for token in ("time", "latency", "memory", "cost", "size")):
        return "efficiency"
    if any(token in normalized for token in ("f1", "accuracy", "precision", "recall", "auc", "mrr", "ndcg", "hit_rate")):
        return "performance"
    return normalized


def _select_primary_figure_groups(
    grouped: dict[tuple[str, str], list[AggregateRecord]],
    *,
    findings: list[FindingRecord],
    budget: int,
) -> list[tuple[tuple[str, str], list[AggregateRecord]]]:
    """Choose a small claim-oriented set instead of experiment × metric plots."""

    if budget <= 0:
        return []
    finding_keys = {
        (experiment_id, metric)
        for finding in findings
        for experiment_id in finding.evidence_experiment_ids
        for metric in finding.metric_names
    }
    ranked = sorted(
        (
            (key, items)
            for key, items in grouped.items()
            if len(items) >= 2
        ),
        key=lambda pair: (
            pair[0] not in finding_keys,
            "abl" in pair[0][0].casefold(),
            _metric_priority(pair[0][1]),
            -sum(item.count for item in pair[1]),
            pair[0],
        ),
    )
    selected: list[tuple[tuple[str, str], list[AggregateRecord]]] = []
    selected_keys: set[tuple[str, str]] = set()
    per_experiment_families: dict[str, set[str]] = defaultdict(set)

    # First give each evidence-bearing experiment one decisive performance view.
    for key, items in ranked:
        experiment_id, metric = key
        if _metric_family(metric) != "performance":
            continue
        if "performance" in per_experiment_families[experiment_id]:
            continue
        selected.append((key, items)); selected_keys.add(key)
        per_experiment_families[experiment_id].add("performance")
        if len(selected) >= budget:
            return selected

    # Then add at most one efficiency view per experiment and only afterwards
    # a genuinely different metric family. Correlated score variants remain in
    # the exact table/CSV instead of becoming separate figures.
    for key, items in ranked:
        if key in selected_keys:
            continue
        experiment_id, metric = key
        family = _metric_family(metric)
        if family in per_experiment_families[experiment_id]:
            continue
        selected.append((key, items)); selected_keys.add(key)
        per_experiment_families[experiment_id].add(family)
        if len(selected) >= budget:
            break
    return selected


def build_visualization_package(
    metric_records: list[MetricRecord],
    aggregates: list[AggregateRecord],
    analyses: list[AnalysisRecord],
    findings: list[FindingRecord],
    run_dir: Path,
    *,
    domain_name: str,
    primary_dataset: str,
    baseline_names: set[str] | None = None,
    minimum_figure_candidates: int = 4,
    maximum_figure_candidates: int = 8,
) -> tuple[
    list[VisualizationDataAsset],
    list[VisualizationCandidate],
    dict[str, str],
    dict[str, str],
    list[TableArtifact],
    list[FigureArtifact],
    str,
]:
    visualization_dir = run_dir / "visualization"
    raw_path = visualization_dir / "raw_metrics.csv"
    aggregate_path = visualization_dir / "aggregated_metrics.csv"
    raw_rows = [
        {
            "record_id": item.record_id,
            "experiment_id": item.experiment_id,
            "dataset": item.dataset,
            "method": item.method,
            "metric": item.metric,
            "value": item.value,
            "unit": item.unit or "",
            "seed": "" if item.seed is None else item.seed,
            "split": item.split,
            "parameters": json.dumps(
                item.parameters, ensure_ascii=False, sort_keys=True
            ),
        }
        for item in metric_records
    ]
    aggregate_rows = [
        {
            "experiment_id": item.experiment_id,
            "dataset": item.dataset,
            "method": item.method,
            "metric": item.metric,
            "parameters": json.dumps(
                item.parameters, ensure_ascii=False, sort_keys=True
            ),
            "direction": item.direction,
            "unit": item.unit or "",
            "count": item.count,
            "mean": item.mean,
            "std": item.std,
            "minimum": item.minimum,
            "maximum": item.maximum,
            "ci95_low": item.ci95_low,
            "ci95_high": item.ci95_high,
        }
        for item in aggregates
    ]
    raw_count = write_csv_atomic(
        raw_path,
        [
            "record_id",
            "experiment_id",
            "dataset",
            "method",
            "metric",
            "value",
            "unit",
            "seed",
            "split",
            "parameters",
        ],
        raw_rows,
    )
    aggregate_count = write_csv_atomic(
        aggregate_path,
        [
            "experiment_id",
            "dataset",
            "method",
            "metric",
            "parameters",
            "direction",
            "unit",
            "count",
            "mean",
            "std",
            "minimum",
            "maximum",
            "ci95_low",
            "ci95_high",
        ],
        aggregate_rows,
    )
    experiment_ids = sorted({item.experiment_id for item in metric_records})
    metric_record_ids = [item.record_id for item in metric_records]
    data_assets = [
        VisualizationDataAsset(
            data_id="VD-RAW-METRICS",
            path=relative_to_run(raw_path, run_dir),
            format="csv",
            data_level="RAW",
            column_schema={
                "record_id": "string",
                "experiment_id": "string",
                "dataset": "string",
                "method": "string",
                "metric": "string",
                "value": "float",
                "unit": "string|null",
                "seed": "integer|null",
                "split": "string",
                "parameters": "json_object",
            },
            units={"value": None},
            row_count=raw_count,
            source_experiment_ids=experiment_ids,
            source_metric_record_ids=metric_record_ids,
            aggregation=None,
            description="每条成功运行指标一行，未按候选图表汇总的绘图源数据",
        ),
        VisualizationDataAsset(
            data_id="VD-AGGREGATED-METRICS",
            path=relative_to_run(aggregate_path, run_dir),
            format="csv",
            data_level="AGGREGATED",
            column_schema={
                "experiment_id": "string",
                "dataset": "string",
                "method": "string",
                "metric": "string",
                "parameters": "json_object",
                "direction": "string",
                "unit": "string|null",
                "count": "integer",
                "mean": "float",
                "std": "float",
                "minimum": "float",
                "maximum": "float",
                "ci95_low": "float",
                "ci95_high": "float",
            },
            units={"mean": None, "std": None, "ci95_low": None, "ci95_high": None},
            row_count=aggregate_count,
            source_experiment_ids=experiment_ids,
            source_metric_record_ids=metric_record_ids,
            aggregation=(
                "按experiment_id、dataset、method、parameters、metric聚合成功test运行；"
                "均值与95% bootstrap区间"
            ),
            description="供表格和点区间图直接使用的聚合结果",
        ),
    ]

    candidates: list[VisualizationCandidate] = []
    tables: dict[str, str] = {}
    figures: dict[str, str] = {}
    table_artifacts: list[TableArtifact] = []
    figure_artifacts: list[FigureArtifact] = []
    analysis_ids = [item.analysis_id for item in analyses]
    finding_ids = [item.finding_id for item in findings]

    table_path = run_dir / "tables" / "summary-results.md"
    table_content = _build_markdown_table(aggregates)
    write_text_atomic(table_path, table_content)
    table_spec_path = visualization_dir / "candidate-summary-table.json"
    table_design = {
        "columns": [
            "experiment_id",
            "dataset",
            "method",
            "parameters",
            "metric",
            "mean",
            "ci95",
            "count",
        ],
        "sort": ["experiment_id", "dataset", "metric", "method", "parameters"],
        "number_format": ".4g",
        "source": "VD-AGGREGATED-METRICS",
    }
    write_json_atomic(table_spec_path, table_design)
    tables["summary_results"] = table_content
    table_artifacts.append(
        TableArtifact(
            name="summary_results",
            path=relative_to_run(table_path, run_dir),
            source_experiment_ids=experiment_ids,
        )
    )
    candidates.append(
        VisualizationCandidate(
            candidate_id="VC-SUMMARY-TABLE",
            kind="TABLE",
            visualization_type="multi_dataset_metric_summary_table",
            title="主实验、基线与消融结果汇总",
            purpose="在一个可核对的表中保留全部方法、数据集、指标和不确定性",
            domain_rationale=(
                f"{domain_name}任务包含多方法或多指标比较，表格适合精确核对论文数字"
            ),
            source_data_ids=["VD-RAW-METRICS", "VD-AGGREGATED-METRICS"],
            source_experiment_ids=experiment_ids,
            analysis_ids=analysis_ids,
            related_finding_ids=finding_ids,
            design_spec=table_design,
            priority="PRIMARY",
            recommended_for=["main_text", "appendix"],
            preview_path=relative_to_run(table_path, run_dir),
            editable_spec_path=relative_to_run(table_spec_path, run_dir),
            caption_draft="各方法在成功test运行上的均值、95% bootstrap区间与运行次数。",
            limitations=["不同指标的数值尺度不可直接横向比较"],
        )
    )

    # A candidate gallery is an evidence budget, not a quota.  Plans may still
    # request an oversized gallery; cap the main-paper candidate set so one
    # experiment × metric Cartesian product cannot flood Module 4 with dozens
    # of near-duplicate plots.  All metrics remain available in the source CSV
    # and exact summary table.
    effective_maximum = min(maximum_figure_candidates, 8)
    effective_minimum = min(minimum_figure_candidates, effective_maximum)
    grouped_aggregates: dict[tuple[str, str], list[AggregateRecord]] = defaultdict(list)
    for item in aggregates:
        if item.dataset == primary_dataset:
            grouped_aggregates[(item.experiment_id, item.metric)].append(item)

    selected_groups = _select_primary_figure_groups(
        grouped_aggregates,
        findings=findings,
        budget=effective_minimum,
    )
    for metric_index, ((experiment_id, metric), items) in enumerate(
        selected_groups,
        start=1,
    ):
        if not items:
            continue
        candidate_slug = (
            f"{metric_index}-{slugify(experiment_id)}-{slugify(metric)}"
        )
        raw_items = [
            record
            for record in metric_records
            if record.experiment_id == experiment_id
            and record.dataset == primary_dataset
            and record.metric == metric
        ]
        visualization_type = _select_visualization_type(items)
        display_label_map = {}
        for item in items:
            full_label = _series_label(item)
            display = compact_label(_display_label(full_label))
            if display != full_label:
                display_label_map[display] = full_label
        figure_name = f"primary_{candidate_slug}_{visualization_type}"
        svg_path = run_dir / "figures" / "candidates" / f"{figure_name}.svg"
        pdf_path = svg_path.with_suffix(".pdf")
        png_path = svg_path.with_suffix(".png")
        tiff_path = svg_path.with_suffix(".tiff")
        # Keep the headline minimal; experiment, dataset, uncertainty and run
        # count belong in the subtitle/caption rather than consuming plot area.
        title = _display_label(metric)
        try:
            render_metadata = _render_comparison_figure(
                title=title,
                experiment_id=experiment_id,
                dataset=primary_dataset,
                metric=metric,
                items=items,
                raw_items=raw_items,
                svg_path=svg_path,
                pdf_path=pdf_path,
                png_path=png_path,
                tiff_path=tiff_path,
            )
        except ValueError as exc:
            # One candidate failing export QA must not abort the whole analysis:
            # skip the candidate (its files stay on disk for inspection) and keep
            # rendering the rest of the catalogue.
            logging.getLogger(__name__).warning(
                "跳过导出 QA 不合格的候选图 %s（%s）", figure_name, exc
            )
            continue
        design_path = visualization_dir / f"candidate-{candidate_slug}.json"
        design = {
            "mark": visualization_type,
            "x": "mean",
            "y": "method",
            "interval": (
                ["ci95_low", "ci95_high"]
                if render_metadata["uncertainty_available"]
                else None
            ),
            "raw_points": "value by seed",
            "facet": None,
            "filter": {
                "experiment_id": experiment_id,
                "dataset": primary_dataset,
                "metric": metric,
            },
            "axis_policy": render_metadata["axis_policy"],
            "degenerate_evidence": render_metadata["degenerate_evidence"],
            "export_paths": {
                "editable_svg": relative_to_run(svg_path, run_dir),
                "vector_pdf": relative_to_run(pdf_path, run_dir),
                "preview_png": relative_to_run(png_path, run_dir),
                "submission_tiff": relative_to_run(tiff_path, run_dir),
            },
            "qa_path": relative_to_run(svg_path.with_suffix(".qa.json"), run_dir),
            "style": {
                "backend": "matplotlib",
                "font_family": "/".join(publication_font_stack()),
                "minimum_font_size_pt": 7.2,
                "palette": "stable method mapping: teal hero, blue comparator, neutral tree",
                "grid": "light major grid on the value axis",
                "direct_labels": True,
            },
            "display_label_map": display_label_map,
            "source": "VD-AGGREGATED-METRICS",
        }
        write_json_atomic(design_path, design)
        related_findings = [
            item.finding_id
            for item in findings
            if metric in item.metric_names
            and experiment_id in item.evidence_experiment_ids
        ]
        related_analyses = [
            item.analysis_id
            for item in analyses
            if metric in item.purpose
            and primary_dataset in item.purpose
            and experiment_id in item.source_experiment_ids
        ]
        source_ids = [experiment_id]
        uncertainty_text = (
            "逐次运行值、均值和95% bootstrap区间"
            if render_metadata["uncertainty_available"]
            else "真实单次观测值；不虚构置信区间"
        )
        description = (
            f"实验{experiment_id}在{primary_dataset}上各方法{metric}的论文级候选图；"
            f"展示{uncertainty_text}"
        )
        figures[figure_name] = description
        figure_artifacts.append(
            FigureArtifact(
                name=figure_name,
                path=relative_to_run(svg_path, run_dir),
                caption=f"各方法{metric}的{uncertainty_text}。",
                source_experiment_ids=source_ids,
                displayed_metrics=[metric],
                caption_assertions=[
                    "marks encode only verified successful-run values",
                    ("intervals encode 95% bootstrap intervals" if render_metadata["uncertainty_available"] else "no uncertainty interval is claimed"),
                ],
                source_data_ids=["VD-RAW-METRICS", "VD-AGGREGATED-METRICS"],
                evidence_role="primary comparison" if metric_index == 1 else "supporting comparison",
                display_label_map=display_label_map,
            )
        )
        candidates.append(
            VisualizationCandidate(
                candidate_id=f"VC-{candidate_slug.upper()}-{visualization_type.upper()}",
                kind="FIGURE",
                visualization_type=visualization_type,
                title=title,
                purpose=(
                    "比较方法中心趋势，同时展示跨运行不确定性"
                    if render_metadata["uncertainty_available"]
                    else "比较当前真实观测，不暗示尚不存在的重复实验不确定性"
                ),
                domain_rationale=_domain_rationale(domain_name, metric),
                source_data_ids=["VD-RAW-METRICS", "VD-AGGREGATED-METRICS"],
                source_experiment_ids=source_ids,
                analysis_ids=related_analyses,
                related_finding_ids=related_findings,
                design_spec=design,
                priority="PRIMARY" if metric_index == 1 else "SUPPORTING",
                recommended_for=["main_text"],
                preview_path=relative_to_run(svg_path, run_dir),
                editable_spec_path=relative_to_run(design_path, run_dir),
                caption_draft=(
                    f"各方法{metric}的{uncertainty_text}；仅统计成功test运行。"
                ),
                limitations=[
                    "仅展示主数据集；其他数据集结果见候选汇总表",
                    "运行次数较少时bootstrap区间只能反映当前重复实验，不能替代更大样本验证",
                ],
            )
        )

    catalog_candidates, catalog_figures, catalog_artifacts = (
        build_catalog_candidates(
            metric_records,
            aggregates,
            analyses,
            findings,
            run_dir,
            primary_dataset=primary_dataset,
            baseline_names=set(baseline_names or set()),
            existing_figure_count=len(figure_artifacts),
            minimum_figure_candidates=effective_minimum,
            maximum_figure_candidates=effective_maximum,
        )
    )
    candidates.extend(catalog_candidates)
    figures.update(catalog_figures)
    figure_artifacts.extend(catalog_artifacts)
    writing_brief_path = run_dir / "outputs" / "writing-evidence.md"
    write_text_atomic(
        writing_brief_path,
        _build_writing_brief(
            aggregates,
            analyses,
            findings,
            candidates,
        ),
    )

    return (
        data_assets,
        candidates,
        tables,
        figures,
        table_artifacts,
        figure_artifacts,
        relative_to_run(writing_brief_path, run_dir),
    )


def _build_markdown_table(aggregates: list[AggregateRecord]) -> str:
    lines = [
        "| Experiment | Dataset | Method | Parameters | Metric | Mean | 95% CI | N |",
        "|---|---|---|---|---|---:|---:|---:|",
    ]
    for item in aggregates:
        parameters = _escape_markdown_cell(
            json.dumps(item.parameters, ensure_ascii=False, sort_keys=True)
        )
        lines.append(
            f"| {_escape_markdown_cell(item.experiment_id)} | "
            f"{_escape_markdown_cell(item.dataset)} | "
            f"{_escape_markdown_cell(item.method)} | {parameters} | "
            f"{_escape_markdown_cell(item.metric)} | "
            f"{item.mean:.6g} | [{item.ci95_low:.6g}, {item.ci95_high:.6g}] | "
            f"{item.count} |"
        )
    return "\n".join(lines) + "\n"


def _build_writing_brief(
    aggregates: list[AggregateRecord],
    analyses: list[AnalysisRecord],
    findings: list[FindingRecord],
    candidates: list[VisualizationCandidate],
) -> str:
    experiment_ids = sorted({item.experiment_id for item in aggregates})
    datasets = sorted({item.dataset for item in aggregates})
    methods = sorted({item.method for item in aggregates})
    metrics = sorted({item.metric for item in aggregates})
    lines = [
        "# Module 3 writing evidence brief",
        "",
        "本文件只整理真实执行产物，供模块四撰写实验部分；不得把候选图数量当成独立证据数量。",
        "",
        "## Evidence coverage",
        "",
        f"- Experiments: {len(experiment_ids)} ({', '.join(experiment_ids)})",
        f"- Datasets: {len(datasets)} ({', '.join(datasets)})",
        f"- Methods/variants: {len(methods)} ({', '.join(methods)})",
        f"- Metrics: {len(metrics)} ({', '.join(metrics)})",
        f"- Successful aggregate cells: {len(aggregates)}",
        "",
        "## Evidence-backed findings",
        "",
    ]
    lines.extend(
        f"- **{item.finding_id}** {item.statement} "
        f"(experiments: {', '.join(item.evidence_experiment_ids)}; "
        f"metrics: {', '.join(item.metric_names)})"
        for item in findings
    )
    lines.extend(["", "## Full aggregate results", "", _build_markdown_table(aggregates).rstrip(), "", "## Analysis and limitations", ""])
    for item in analyses:
        lines.append(f"### {item.analysis_id}: {item.method_name}")
        lines.append("")
        lines.append(item.summary)
        lines.append("")
        lines.append(f"- Purpose: {item.purpose}")
        lines.append(f"- Parameters: `{json.dumps(item.parameters, ensure_ascii=False, sort_keys=True)}`")
        lines.append(
            "- Limitations: "
            + ("; ".join(item.limitations) if item.limitations else "none recorded")
        )
        lines.append("")
    lines.extend(["## Candidate figure routing", ""])
    for priority in ("PRIMARY", "SUPPORTING", "OPTIONAL"):
        selected = [item for item in candidates if item.kind == "FIGURE" and item.priority == priority]
        lines.append(f"### {priority}")
        lines.append("")
        if not selected:
            lines.append("- None")
        else:
            lines.extend(
                f"- **{item.candidate_id}** {item.title}: {item.purpose} "
                f"(recommended: {', '.join(item.recommended_for)})"
                for item in selected
            )
        lines.append("")
    lines.extend(
        [
            "## Writing guardrails",
            "",
            "- 优先使用PRIMARY候选建立主结论，SUPPORTING用于稳定性和边界，OPTIONAL放附录。",
            "- 同一批指标的不同画法是互补视角，不得写成多份独立实验证据。",
            "- 未支持假设和失败运行必须披露；不得删除不利结果。",
            "- 所有数字以experiment-module-output.json和CSV为准，禁止从图片反读数值。",
            "",
        ]
    )
    return "\n".join(lines)


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _select_visualization_type(items: list[AggregateRecord]) -> str:
    if items and any(item.count < 2 for item in items):
        return "single_run_point_comparison"
    values = [value for item in items for value in (item.mean, item.ci95_low, item.ci95_high)]
    if values and max(values) - min(values) <= 1e-12:
        return "observed_equivalence_dotplot"
    return "raw_point_interval_comparison"


def _render_comparison_figure(
    *,
    title: str,
    experiment_id: str,
    dataset: str,
    metric: str,
    items: list[AggregateRecord],
    raw_items: list[MetricRecord],
    svg_path: Path,
    pdf_path: Path,
    png_path: Path,
    tiff_path: Path,
) -> dict[str, object]:
    """Render one deterministic, source-linked, publication-grade candidate."""

    svg_path.parent.mkdir(parents=True, exist_ok=True)
    visualization_type = _select_visualization_type(items)
    degenerate = visualization_type == "observed_equivalence_dotplot"
    uncertainty_available = bool(items) and all(item.count >= 2 for item in items)
    ordered_items = sorted(
        items,
        key=lambda item: item.mean,
        reverse=items[0].direction == "MAXIMIZE",
    )
    raw_by_series = _raw_values_by_series(raw_items)
    axis_limits = _metric_axis_limits(metric, ordered_items, raw_by_series)
    height = max(2.75, 1.45 + 0.58 * len(ordered_items))
    # 宽度随最长刻度标签增长（2026-09-17 修）：标签形如
    # "BM25 Sparse (confidence=off,gate=off,recency=off,top K=10)" 会很长，
    # constrained_layout 会把数据轴压到几十 pt，x 轴刻度文字互相重叠，
    # 导出 QA（pdf_audit）判定 FAIL 并直接抛错，让整个分析阶段崩掉。
    # 标签已由 compact_label 压到 TICK_LABEL_LIMIT 以内，这里按实际显示长度
    # （7.4pt 字体实测约 0.077 英寸/字符）预留标签列宽。
    longest_label = max(
        (len(compact_label(_display_label(_series_label(item)))) for item in ordered_items),
        default=0,
    )
    width = min(14.0, max(6.8, 4.2 + 0.077 * longest_label))
    with plt.rc_context(publication_rc(font_size=8.0)):
        figure, axis = plt.subplots(figsize=(width, height))
        figure.subplots_adjust(left=0.27, right=0.94, bottom=0.22, top=0.72)
        figure.patch.set_facecolor("white")
        axis.set_facecolor("white")
        positions = list(range(len(ordered_items)))
        for index, item in enumerate(ordered_items):
            label = _series_label(item)
            raw_values = raw_by_series.get(label, [])
            color = method_color(label, index)
            if raw_values:
                offsets = _symmetric_offsets(len(raw_values), width=0.13)
                axis.scatter(
                    raw_values,
                    [index + offset for offset in offsets],
                    s=21,
                    color=color,
                    alpha=0.28,
                    edgecolors="none",
                    zorder=2,
                    clip_on=False,
                )
            if uncertainty_available:
                lower = max(0.0, item.mean - item.ci95_low)
                upper = max(0.0, item.ci95_high - item.mean)
                axis.errorbar(
                    item.mean, index, xerr=[[lower], [upper]], fmt="o",
                    markersize=6.8, markerfacecolor=color, markeredgecolor="white",
                    markeredgewidth=0.9, ecolor=color, elinewidth=1.7,
                    capsize=3.4, capthick=1.25, zorder=3, clip_on=False,
                )
                value_label = f"{item.mean:.3f}  [{item.ci95_low:.3f}, {item.ci95_high:.3f}]"
            else:
                axis.scatter(
                    item.mean, index, s=54, color=color, edgecolors="white",
                    linewidths=0.9, zorder=3, clip_on=False,
                )
                value_label = f"{item.mean:.3f}"
            axis.annotate(
                value_label,
                xy=(item.mean, index),
                xycoords="data",
                xytext=(8, 0),
                textcoords="offset points",
                va="center",
                ha="left",
                fontsize=7.2,
                color="#263238",
                clip_on=False,
            )

        axis.set_yticks(
            positions,
            [
                compact_label(_display_label(_series_label(item)))
                for item in ordered_items
            ],
        )
        axis.invert_yaxis()
        axis.set_xlabel(_display_label(metric))
        if axis_limits is not None:
            axis.set_xlim(*axis_limits)
            axis.xaxis.set_major_locator(MaxNLocator(5, prune=None))
            axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.2f}"))
            axis_policy = (
                f"data-focused view within valid metric domain "
                f"[{axis_limits[0]:.4g}, {axis_limits[1]:.4g}]"
            )
        else:
            lows = [min(item.minimum, item.ci95_low) for item in ordered_items]
            highs = [max(item.maximum, item.ci95_high) for item in ordered_items]
            span = max(highs) - min(lows)
            padding = span * 0.14 if span > 0 else max(abs(max(highs)) * 0.08, 0.08)
            axis.set_xlim(min(lows) - padding, max(highs) + 2.4 * padding)
            axis.xaxis.set_major_locator(MaxNLocator(5))
            axis_policy = "data range with symmetric padding; no forced zero"
        polish_axis(axis, grid_axis="x")
        axis.spines[["top", "right", "left"]].set_visible(False)
        axis.tick_params(axis="y", length=0, pad=8)
        if uncertainty_available:
            subtitle = (
                f"{experiment_id} · {_display_label(dataset)} · "
                f"mean [95% bootstrap CI] · dots are runs · "
                f"n={ordered_items[0].count} per method"
            )
        else:
            subtitle = (
                f"{experiment_id} · {_display_label(dataset)} · observed value · "
                "one successful run per method; uncertainty not estimable"
            )
        figure.text(
            0.06,
            0.94,
            title,
            ha="left",
            va="top",
            fontsize=10.3,
            weight="bold",
            color="#17242D",
        )
        figure.text(
            0.06,
            0.84,
            subtitle,
            ha="left",
            va="top",
            fontsize=7.2,
            color="#66727D",
        )
        if degenerate:
            axis.text(
                0,
                -0.22,
                "No observed difference in the available runs; overlapping values are shown without exaggerating the axis.",
                transform=axis.transAxes,
                ha="left",
                va="top",
                fontsize=7.2,
                color="#475569",
            )
        figure.savefig(
            svg_path,
            format="svg",
            metadata={"Date": None, "Creator": "JiuwenSwarm Module 3"},
            bbox_inches="tight",
        )
        figure.savefig(
            pdf_path,
            format="pdf",
            metadata={
                "CreationDate": None,
                "ModDate": None,
                "Creator": "JiuwenSwarm Module 3",
            },
            bbox_inches="tight",
        )
        figure.savefig(
            png_path,
            format="png",
            dpi=300,
            metadata={"Software": "JiuwenSwarm Module 3"},
            bbox_inches="tight",
        )
        figure.savefig(
            tiff_path,
            format="tiff",
            dpi=600,
            bbox_inches="tight",
            pil_kwargs={"compression": "tiff_lzw"},
        )
        plt.close(figure)
    write_json_atomic(
        svg_path.with_suffix(".qa.json"),
        audit_figure_exports(svg_path.with_suffix("")),
    )
    return {
        "axis_policy": axis_policy,
        "degenerate_evidence": degenerate,
        "uncertainty_available": uncertainty_available,
    }


def _metric_bounds(metric: str) -> tuple[float, float] | None:
    normalized = metric.casefold().replace("-", "_")
    bounded_tokens = ("f1", "accuracy", "acc", "precision", "recall", "auc", "rate")
    if any(token in normalized for token in bounded_tokens):
        return (0.0, 1.0)
    return None


def _metric_axis_limits(
    metric: str,
    items: list[AggregateRecord],
    raw_by_series: dict[str, list[float]],
) -> tuple[float, float] | None:
    """Use an honest but readable window for naturally bounded metrics."""

    valid_bounds = _metric_bounds(metric)
    if valid_bounds is None:
        return None
    valid_low, valid_high = valid_bounds
    evidence: list[float] = []
    for item in items:
        evidence.extend(
            [item.minimum, item.maximum, item.ci95_low, item.ci95_high, item.mean]
        )
        evidence.extend(raw_by_series.get(_series_label(item), []))
    if not evidence:
        return valid_bounds
    observed_low, observed_high = min(evidence), max(evidence)
    observed_span = max(observed_high - observed_low, 0.02)
    lower = max(valid_low, observed_low - 0.18 * observed_span)
    upper = min(valid_high, observed_high + 0.28 * observed_span)
    minimum_visible_span = min(0.10, valid_high - valid_low)
    if upper - lower < minimum_visible_span:
        center = (observed_low + observed_high) / 2
        lower = max(valid_low, center - minimum_visible_span / 2)
        upper = min(valid_high, lower + minimum_visible_span)
        lower = max(valid_low, upper - minimum_visible_span)
    return lower, upper


def _raw_values_by_series(records: list[MetricRecord]) -> dict[str, list[float]]:
    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for item in records:
        label = item.method
        if item.parameters:
            values = ",".join(
                f"{key}={value}" for key, value in sorted(item.parameters.items())
            )
            label = f"{item.method} ({values})"
        grouped[label].append((item.seed if item.seed is not None else -1, item.value))
    return {
        label: [value for _, value in sorted(values)]
        for label, values in grouped.items()
    }


def _symmetric_offsets(count: int, *, width: float) -> list[float]:
    if count <= 1:
        return [0.0]
    return [(-width / 2) + width * index / (count - 1) for index in range(count)]


def _direct_label_position(
    evidence_low: float,
    evidence_high: float,
    *,
    bounded: tuple[float, float] | None,
) -> tuple[float, str]:
    if bounded is not None:
        lower, upper = bounded
        # Keep direct labels clear of interval caps and raw-point marker edges.
        # The rendered-PDF collision audit needs more separation than the
        # annotation's anchor alone suggests because glyph boxes extend left.
        gap = max((upper - lower) * 0.08, 0.02)
        if upper - evidence_high >= (upper - lower) * 0.16:
            return min(upper - gap, evidence_high + gap), "left"
        return max(lower + gap, evidence_low - gap), "right"
    span = max(evidence_high - evidence_low, abs(evidence_high) * 0.04, 0.04)
    return evidence_high + span * 0.35, "left"


def _display_label(value: str) -> str:
    return display_label(value)


def _series_label(item: AggregateRecord) -> str:
    if not item.parameters:
        return item.method
    values = ",".join(f"{key}={value}" for key, value in sorted(item.parameters.items()))
    return f"{item.method} ({values})"


def _domain_rationale(domain_name: str, metric: str) -> str:
    lower = metric.lower()
    if any(token in lower for token in ("latency", "cost", "time", "memory")):
        return f"{domain_name}任务需要比较效率指标，点和区间能同时呈现水平与运行波动"
    if any(token in lower for token in ("f1", "precision", "recall", "accuracy", "auc")):
        return f"{domain_name}评测包含分类型指标，点区间图便于比较方法差异而不隐藏不确定性"
    return f"{domain_name}任务当前输出为重复运行标量指标，点区间图与现有证据粒度匹配"
