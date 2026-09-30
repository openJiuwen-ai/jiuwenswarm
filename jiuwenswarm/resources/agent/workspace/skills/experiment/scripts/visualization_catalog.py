"""Evidence-role visualization catalog for writing-rich experiment delivery."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Callable

from matplotlib import pyplot as plt
from matplotlib.ticker import MaxNLocator

from contracts import (
    AnalysisRecord,
    FigureArtifact,
    FindingRecord,
    MetricRecord,
    VisualizationCandidate,
)
from io_utils import relative_to_run, slugify, write_json_atomic
from runtime_models import AggregateRecord

from figure_style import (
    compact_label,
    display_label,
    method_color,
    polish_axis,
    publication_font_stack,
    publication_rc,
)


_KINDS = (
    "raw_seed_strip",
    "seed_trajectory",
    "distribution_box",
    "empirical_cdf",
    "rank_by_seed",
    "mean_variability",
    "observed_range",
    "confidence_interval_width",
    "successful_run_count",
    "paired_seed_delta",
    "paired_slope",
    "gap_to_best",
    "stability_score",
)


def build_catalog_candidates(
    metric_records: list[MetricRecord],
    aggregates: list[AggregateRecord],
    analyses: list[AnalysisRecord],
    findings: list[FindingRecord],
    run_dir: Path,
    *,
    primary_dataset: str,
    baseline_names: set[str],
    existing_figure_count: int,
    minimum_figure_candidates: int,
    maximum_figure_candidates: int,
) -> tuple[list[VisualizationCandidate], dict[str, str], list[FigureArtifact]]:
    """Render complementary candidates until the configured evidence budget is met."""

    if minimum_figure_candidates > maximum_figure_candidates:
        raise ValueError("minimum figure target cannot exceed maximum figure budget")

    candidates: list[VisualizationCandidate] = []
    figures: dict[str, str] = {}
    artifacts: list[FigureArtifact] = []
    # Add only enough complementary roles to reach the evidence target.  The
    # maximum is a safety ceiling, not an instruction to fill a gallery.
    target = min(minimum_figure_candidates, maximum_figure_candidates)
    remaining = max(0, target - existing_figure_count)
    if remaining == 0:
        return candidates, figures, artifacts

    grouped: dict[tuple[str, str, str], list[AggregateRecord]] = defaultdict(list)
    for item in aggregates:
        grouped[(item.experiment_id, item.dataset, item.metric)].append(item)
    ordered_groups = sorted(
        grouped.items(),
        key=lambda pair: (
            pair[0][1] != primary_dataset,
            -sum(item.count for item in pair[1]),
            pair[0],
        ),
    )

    for (experiment_id, dataset, metric), items in ordered_groups:
        raw_items = [
            record
            for record in metric_records
            if record.experiment_id == experiment_id
            and record.dataset == dataset
            and record.metric == metric
        ]
        related_analyses = [
            item.analysis_id
            for item in analyses
            if experiment_id in item.source_experiment_ids
        ]
        related_findings = [
            item.finding_id
            for item in findings
            if experiment_id in item.evidence_experiment_ids
            and metric in item.metric_names
        ]
        if not related_analyses or not related_findings:
            continue
        context = _PlotContext(
            experiment_id=experiment_id,
            dataset=dataset,
            metric=metric,
            items=sorted(items, key=lambda item: _series_label(item).casefold()),
            raw_items=raw_items,
            baseline_names=baseline_names,
        )
        display_label_map = {}
        for aggregate in context.items:
            full_label = _series_label(aggregate)
            display = compact_label(_display(full_label))
            if display != full_label:
                display_label_map[display] = full_label
        for kind in _candidate_kind_order(context):
            if len(candidates) >= remaining:
                break
            if not _is_applicable(kind, context):
                continue
            slug = slugify(f"{experiment_id}-{dataset}-{metric}-{kind}")
            figure_name = f"evidence_{slug}"
            base_path = run_dir / "figures" / "candidates" / figure_name
            try:
                metadata = _render(kind, context, base_path)
            except ValueError as exc:
                # A single renderer failing export QA must not abort the whole
                # catalogue: move on to the next applicable candidate kind.
                logging.getLogger(__name__).warning(
                    "跳过导出 QA 不合格的候选图 %s（%s）", figure_name, exc
                )
                continue
            design_path = run_dir / "visualization" / f"candidate-{slug}.json"
            design = {
                "mark": metadata["visualization_type"],
                "evidence_role": metadata["evidence_role"],
                "transform": metadata["transform"],
                "filter": {
                    "experiment_id": experiment_id,
                    "dataset": dataset,
                    "metric": metric,
                },
                "uncertainty": metadata["uncertainty"],
                "source": ["VD-RAW-METRICS", "VD-AGGREGATED-METRICS"],
                "export_paths": _export_paths(base_path, run_dir),
                "qa_path": relative_to_run(base_path.with_suffix(".qa.json"), run_dir),
                "style": {
                    "backend": "matplotlib",
                    "font_family": "/".join(publication_font_stack()),
                    "minimum_font_size_pt": 7.0,
                    "editable_svg_text": True,
                    "palette": "stable method mapping: teal hero, blue comparator, neutral tree",
                    "single_panel_alignment_gate": "NOT_APPLICABLE",
                },
                "display_label_map": display_label_map,
            }
            write_json_atomic(design_path, design)
            relative_svg = relative_to_run(base_path.with_suffix(".svg"), run_dir)
            caption = metadata["caption"]
            candidates.append(
                VisualizationCandidate(
                    candidate_id=f"VC-{slug.upper()}",
                    kind="FIGURE",
                    visualization_type=metadata["visualization_type"],
                    title=metadata["title"],
                    purpose=metadata["purpose"],
                    domain_rationale=metadata["domain_rationale"],
                    source_data_ids=["VD-RAW-METRICS", "VD-AGGREGATED-METRICS"],
                    source_experiment_ids=[experiment_id],
                    analysis_ids=related_analyses,
                    related_finding_ids=related_findings,
                    design_spec=design,
                    priority=metadata["priority"],
                    recommended_for=metadata["recommended_for"],
                    preview_path=relative_svg,
                    editable_spec_path=relative_to_run(design_path, run_dir),
                    caption_draft=caption,
                    limitations=metadata["limitations"],
                )
            )
            figures[figure_name] = metadata["purpose"]
            artifacts.append(
                FigureArtifact(
                    name=figure_name,
                    path=relative_svg,
                    caption=caption,
                    source_experiment_ids=[experiment_id],
                    displayed_metrics=[metric],
                    caption_assertions=[
                        "marks encode only verified successful-run records",
                        str(metadata.get("aggregation") or "aggregation is declared in the candidate design"),
                    ],
                    source_data_ids=["VD-RAW-METRICS", "VD-AGGREGATED-METRICS"],
                    evidence_role=str(metadata.get("purpose") or "supporting evidence"),
                    display_label_map=display_label_map,
                )
            )
        if len(candidates) >= remaining:
            break
    return candidates, figures, artifacts


class _PlotContext:
    def __init__(
        self,
        *,
        experiment_id: str,
        dataset: str,
        metric: str,
        items: list[AggregateRecord],
        raw_items: list[MetricRecord],
        baseline_names: set[str],
    ) -> None:
        self.experiment_id = experiment_id
        self.dataset = dataset
        self.metric = metric
        self.items = items
        self.raw_items = raw_items
        self.baseline_names = baseline_names
        self.series = [_series_label(item) for item in items]
        self.values = _values_by_series(raw_items)
        self.direction = items[0].direction


def _is_applicable(kind: str, context: _PlotContext) -> bool:
    nonempty = [values for values in context.values.values() if values]
    counts = [len(values) for values in nonempty]
    has_repeats = bool(counts) and all(count >= 2 for count in counts)
    has_distribution = bool(counts) and all(count >= 3 for count in counts)
    has_variation = any(item.maximum - item.minimum > 1e-12 for item in context.items)
    has_uncertainty = any(item.ci95_high - item.ci95_low > 1e-12 for item in context.items)
    if kind in {"paired_seed_delta", "paired_slope"}:
        pair = _comparison_pair(context)
        return pair is not None and len(_paired_values(context, *pair)) >= 2
    if kind in {"seed_trajectory", "rank_by_seed"}:
        return has_repeats and len(context.series) >= 2 and len(_all_seeds(context)) >= 2
    if kind in {"empirical_cdf", "distribution_box"}:
        return has_distribution
    if kind == "raw_seed_strip":
        return has_repeats
    if kind in {"mean_variability", "observed_range", "stability_score"}:
        return has_repeats and has_variation
    if kind == "confidence_interval_width":
        return has_repeats and has_uncertainty
    if kind == "successful_run_count":
        return len(set(counts)) > 1
    if kind == "gap_to_best":
        return has_repeats and len(context.series) >= 2
    return bool(context.items and nonempty)


def _candidate_kind_order(context: _PlotContext) -> tuple[str, ...]:
    """Prioritise evidence roles from metric shape instead of a rigid chart list."""

    normalized = context.metric.casefold().replace("-", "_")
    is_runtime = any(token in normalized for token in ("time", "latency", "memory", "size"))
    if is_runtime:
        preferred = (
            "observed_range",
            "distribution_box",
            "seed_trajectory",
            "mean_variability",
            "paired_seed_delta",
            "gap_to_best",
            "empirical_cdf",
            "raw_seed_strip",
            "rank_by_seed",
            "confidence_interval_width",
            "successful_run_count",
            "paired_slope",
            "stability_score",
        )
    else:
        preferred = (
            "raw_seed_strip",
            "distribution_box",
            "paired_seed_delta",
            "seed_trajectory",
            "observed_range",
            "paired_slope",
            "rank_by_seed",
            "mean_variability",
            "gap_to_best",
            "empirical_cdf",
            "confidence_interval_width",
            "successful_run_count",
            "stability_score",
        )
    return tuple(kind for kind in preferred if kind in _KINDS)


def _render(kind: str, context: _PlotContext, base_path: Path) -> dict[str, object]:
    base_path.parent.mkdir(parents=True, exist_ok=True)
    # 画布宽度自适应（2026-09-17 修）：series 标签带参数后缀时可达 45+ 字符，
    # 固定 6.7 英寸会把旋转后的 x 轴刻度挤在一起互相重叠，导出 QA（pdf_audit）
    # 直接判定 FAIL 并抛错，让整个分析阶段崩掉。按"标签数 × 最长标签投影宽度"
    # 预留画布，保证刻度之间有真实间距。
    series_count = max(len(context.series), 1)
    longest_label = max(
        (len(compact_label(_display(item))) for item in context.series), default=0
    )
    # 标签已由 compact_label 压到上限以内，这里按实际显示长度估算
    # （7.4pt 字体下实测约 0.077 英寸/字符），给刻度之间留出真实间距。
    axes_width_inches = series_count * longest_label * 0.077 * 1.10 + 1.6
    width = min(16.0, max(6.7, axes_width_inches / 0.78))
    with plt.rc_context(publication_rc(font_size=8.0)):
        figure, axis = plt.subplots(figsize=(width, 3.9))
        figure.subplots_adjust(left=0.18, right=0.96, bottom=0.20, top=0.73)
        figure.patch.set_facecolor("white")
        axis.set_facecolor("white")
        metadata = _DRAWERS[kind](axis, context)
        polish_axis(axis, grid_axis=_grid_axis(kind))
        if kind == "mean_variability":
            # Direct labels carry the comparison; grid lines would run through
            # them and add visual noise in this sparse diagnostic plot.
            axis.grid(False)
        figure.text(
            0.09,
            0.94,
            metadata["title"],
            ha="left",
            va="top",
            fontsize=10.5,
            weight="bold",
            color="#17242D",
        )
        figure.text(
            0.09,
            0.845,
            f"{context.experiment_id} · {_display(context.dataset)} · {_display(context.metric)}",
            ha="left",
            va="top",
            fontsize=7.0,
            color="#66727D",
        )
        figure.savefig(
            base_path.with_suffix(".svg"),
            format="svg",
            metadata={"Date": None},
            bbox_inches="tight",
        )
        figure.savefig(
            base_path.with_suffix(".pdf"),
            format="pdf",
            metadata={"CreationDate": None, "ModDate": None},
            bbox_inches="tight",
        )
        figure.savefig(
            base_path.with_suffix(".png"),
            format="png",
            dpi=300,
            bbox_inches="tight",
        )
        figure.savefig(
            base_path.with_suffix(".tiff"),
            format="tiff",
            dpi=600,
            bbox_inches="tight",
            pil_kwargs={"compression": "tiff_lzw"},
        )
        plt.close(figure)
    qa_path = base_path.with_suffix(".qa.json")
    write_json_atomic(qa_path, audit_figure_exports(base_path))
    metadata["qa_path"] = qa_path.name
    return metadata


def _draw_raw_strip(axis, context: _PlotContext) -> dict[str, object]:
    for index, label in enumerate(context.series):
        values = [value for _, value in context.values.get(label, [])]
        offsets = _offsets(len(values))
        color = _color(index, label)
        axis.scatter(
            [index + offset for offset in offsets],
            values,
            s=31,
            color=color,
            alpha=0.72,
            edgecolors="white",
            linewidths=0.7,
            zorder=3,
        )
        if values:
            axis.hlines(
                sum(values) / len(values),
                index - 0.20,
                index + 0.20,
                color=color,
                linewidth=2.2,
                zorder=2,
            )
    axis.set_xticks(range(len(context.series)), [_display(item) for item in context.series])
    _rotate_xticklabels(axis)
    axis.set_ylabel(_display(context.metric))
    _value_limits(axis, context)
    return _meta("seed_strip_plot", "Run-level evidence", "逐次运行分布", "展示每个方法在各seed上的原始结果，检查结果是否由单次运行驱动", "raw values; no aggregation", "individual seed values", "SUPPORTING", ["main_text", "supplement"])


def _draw_seed_trajectory(axis, context: _PlotContext) -> dict[str, object]:
    for index, label in enumerate(context.series):
        pairs = context.values.get(label, [])
        axis.plot(
            [seed for seed, _ in pairs],
            [value for _, value in pairs],
            marker="o",
            ms=4.4,
            lw=1.65,
            color=_color(index, label),
            markeredgecolor="white",
            markeredgewidth=0.6,
            label=_display(label),
        )
    axis.set_xlabel("Random seed")
    axis.set_ylabel(_display(context.metric))
    axis.xaxis.set_major_locator(MaxNLocator(integer=True))
    axis.legend(ncol=min(3, len(context.series)), fontsize=7)
    _value_limits(axis, context)
    return _meta("seed_trajectory", "Seed sensitivity", "随机种子敏感性", "比较不同seed下方法排序和波动是否稳定", "ordered by seed", "individual seed values", "SUPPORTING", ["supplement"])


def _draw_box(axis, context: _PlotContext) -> dict[str, object]:
    values = [[value for _, value in context.values[label]] for label in context.series]
    box = axis.boxplot(
        values,
        patch_artist=True,
        showfliers=False,
        widths=0.50,
        medianprops={"color": "#263238", "linewidth": 1.4},
        whiskerprops={"color": "#7E8994", "linewidth": 1.0},
        capprops={"color": "#7E8994", "linewidth": 1.0},
    )
    axis.set_xticks(
        range(1, len(context.series) + 1),
        [_display(item) for item in context.series],
    )
    for index, patch in enumerate(box["boxes"]):
        patch.set_facecolor(_color(index, context.series[index]))
        patch.set_alpha(0.28)
        patch.set_edgecolor(_color(index, context.series[index]))
        patch.set_linewidth(1.1)
    for index, series in enumerate(values):
        axis.scatter(
            [index + 1 + offset for offset in _offsets(len(series))],
            series,
            s=24,
            color=_color(index, context.series[index]),
            edgecolors="white",
            linewidths=0.55,
            alpha=0.82,
            zorder=3,
        )
    _rotate_xticklabels(axis)
    axis.set_ylabel(_display(context.metric))
    _value_limits(axis, context)
    return _meta("box_with_raw_points", "Distribution shape", "跨seed分布与中位数", "同时展示中位数、四分位范围和全部seed值", "box statistics plus raw points", "median and interquartile range", "SUPPORTING", ["supplement"])


def _draw_ecdf(axis, context: _PlotContext) -> dict[str, object]:
    for index, label in enumerate(context.series):
        values = sorted(value for _, value in context.values[label])
        y = [(item + 1) / len(values) for item in range(len(values))]
        color = _color(index, label)
        axis.step(values, y, where="post", lw=1.7, color=color, label=_display(label))
        axis.scatter(values, y, s=18, color=color, edgecolors="white", linewidths=0.45)
    axis.set_xlabel(_display(context.metric))
    axis.set_ylabel("Empirical cumulative probability")
    axis.set_ylim(0, 1.03)
    axis.legend(fontsize=7)
    return _meta("empirical_cdf", "Distribution dominance", "经验累积分布", "检查一个方法是否在完整seed分布上持续占优，而不只比较均值", "ECDF over successful seeds", "none", "SUPPORTING", ["supplement"])


def _draw_rank(axis, context: _PlotContext) -> dict[str, object]:
    seeds = _all_seeds(context)
    rank_rows = _ranks(context, seeds)
    for index, label in enumerate(context.series):
        ranks = [row.get(label, math.nan) for row in rank_rows]
        axis.plot(
            seeds,
            ranks,
            marker="o",
            ms=4.2,
            lw=1.55,
            color=_color(index, label),
            markeredgecolor="white",
            markeredgewidth=0.5,
            label=_display(label),
        )
    axis.set_xlabel("Random seed")
    axis.set_ylabel("Rank (1 = best)")
    axis.set_yticks(range(1, len(context.series) + 1))
    axis.invert_yaxis()
    axis.legend(
        fontsize=6.8,
        loc="lower center",
        bbox_to_anchor=(0.5, 1.02),
        ncol=min(3, len(context.series)),
        columnspacing=1.3,
        handlelength=1.7,
    )
    return _meta("rank_stability", "Rank robustness", "方法排名稳定性", "展示方法排名是否随随机种子改变", "rank within each common seed", "none", "SUPPORTING", ["main_text", "supplement"])


def _draw_mean_variability(axis, context: _PlotContext) -> dict[str, object]:
    for index, item in enumerate(context.items):
        label = _series_label(item)
        axis.scatter(
            item.std,
            item.mean,
            s=58,
            color=_color(index, label),
            edgecolors="white",
            linewidths=0.8,
            label=_display(label),
        )
        axis.annotate(
            _display(label),
            (item.std, item.mean),
            xytext=(6, 3 if index % 2 == 0 else -8),
            textcoords="offset points",
            fontsize=6.8,
            color="#45515C",
            va="bottom" if index % 2 == 0 else "top",
        )
    axis.set_xlabel("Across-seed standard deviation")
    axis.set_ylabel(f"Mean {_display(context.metric)}")
    _value_limits(axis, context, y_axis=True)
    return _meta("performance_variability_scatter", "Performance–stability trade-off", "性能—稳定性权衡", "识别均值较高但波动也较大的方法", "mean versus across-seed standard deviation", "standard deviation", "SUPPORTING", ["main_text", "supplement"])


def _draw_range(axis, context: _PlotContext) -> dict[str, object]:
    for index, item in enumerate(context.items):
        color = _color(index, _series_label(item))
        axis.plot([item.minimum, item.maximum], [index, index], lw=3.4, color=color, alpha=0.38)
        axis.scatter(item.mean, index, s=43, color=color, edgecolors="white", linewidths=0.7, zorder=3)
    axis.set_yticks(range(len(context.items)), [_display(_series_label(item)) for item in context.items])
    axis.invert_yaxis()
    axis.set_xlabel(_display(context.metric))
    return _meta("min_max_robustness", "Observed robustness envelope", "最坏—最好运行范围", "展示当前重复实验中每种方法的最坏值、最好值和均值", "minimum–maximum with mean", "observed range", "SUPPORTING", ["supplement"])


def _draw_ci_width(axis, context: _PlotContext) -> dict[str, object]:
    widths = [max(0.0, item.ci95_high - item.ci95_low) for item in context.items]
    axis.barh(
        range(len(context.items)),
        widths,
        color=[_color(index, _series_label(item)) for index, item in enumerate(context.items)],
        alpha=0.82,
        edgecolor="white",
        linewidth=0.7,
    )
    axis.set_yticks(range(len(context.items)), [_display(_series_label(item)) for item in context.items])
    axis.invert_yaxis()
    axis.set_xlabel("95% bootstrap CI width")
    return _meta("ci_width", "Estimate precision", "估计不确定性宽度", "比较不同方法均值估计的精确程度", "ci95_high - ci95_low", "95% bootstrap CI width", "OPTIONAL", ["supplement"])


def _draw_count(axis, context: _PlotContext) -> dict[str, object]:
    counts = [item.count for item in context.items]
    axis.bar(
        range(len(counts)),
        counts,
        color=[_color(index, _series_label(item)) for index, item in enumerate(context.items)],
        alpha=0.82,
        edgecolor="white",
        linewidth=0.7,
    )
    axis.set_xticks(
        range(len(counts)),
        [_display(_series_label(item)) for item in context.items],
    )
    _rotate_xticklabels(axis)
    axis.set_ylabel("Successful runs")
    axis.yaxis.set_major_locator(MaxNLocator(integer=True))
    return _meta("run_coverage_bar", "Evidence coverage", "有效重复次数", "披露各方法实际纳入分析的成功运行数，防止不等量比较被忽略", "count of successful test runs", "run count", "OPTIONAL", ["appendix", "supplement"])


def _draw_paired_delta(axis, context: _PlotContext) -> dict[str, object]:
    comparator, primary = _comparison_pair(context) or ("", "")
    pairs = _paired_values(context, comparator, primary)
    deltas = [right - left for _, left, right in pairs]
    if context.direction == "MINIMIZE":
        deltas = [-value for value in deltas]
    colors = ["#2F855A" if value >= 0 else "#B64342" for value in deltas]
    axis.bar(range(len(deltas)), deltas, color=colors, alpha=0.8)
    axis.axhline(0, color="#64748B", lw=0.8)
    axis.set_xticks(range(len(pairs)), [str(seed) for seed, _, _ in pairs])
    axis.set_xlabel("Random seed")
    axis.set_ylabel(f"Improvement in {_display(context.metric)}")
    return _meta("paired_seed_delta", "Paired effect", "相对基线的逐seed增益", "利用相同seed配对，直接展示主方法相对比较方法的增益与反例", f"{primary} minus {comparator}, direction-adjusted", "paired seed difference", "PRIMARY", ["main_text"])


def _draw_paired_slope(axis, context: _PlotContext) -> dict[str, object]:
    comparator, primary = _comparison_pair(context) or ("", "")
    pairs = _paired_values(context, comparator, primary)
    for seed, left, right in pairs:
        color = "#2F855A" if (right >= left) == (context.direction == "MAXIMIZE") else "#B64342"
        axis.plot([0, 1], [left, right], marker="o", ms=3.5, color=color, alpha=0.7, lw=1)
    # Seed labels are deliberately kept in the editable source table rather
    # than stacked outside the axis; the trajectories already encode each
    # paired run and remain readable when several endpoints coincide.
    axis.set_xticks([0, 1], [_display(comparator), _display(primary)])
    axis.set_ylabel(_display(context.metric))
    _value_limits(axis, context)
    return _meta("paired_slopegraph", "Paired consistency", "配对运行变化轨迹", "展示同一seed从比较方法到主方法的方向和幅度是否一致", "paired by identical seed", "individual paired values", "SUPPORTING", ["main_text", "supplement"])


def _draw_gap(axis, context: _PlotContext) -> dict[str, object]:
    best = max(item.mean for item in context.items) if context.direction == "MAXIMIZE" else min(item.mean for item in context.items)
    gaps = [(best - item.mean) if context.direction == "MAXIMIZE" else (item.mean - best) for item in context.items]
    axis.barh(
        range(len(gaps)),
        gaps,
        color=[_color(index, _series_label(item)) for index, item in enumerate(context.items)],
        alpha=0.82,
        edgecolor="white",
        linewidth=0.7,
    )
    axis.set_yticks(range(len(context.items)), [_display(_series_label(item)) for item in context.items])
    axis.invert_yaxis()
    axis.set_xlabel(f"Gap to best {_display(context.metric)}")
    return _meta("gap_to_best", "Relative ranking magnitude", "距最优结果的差距", "量化各方法与当前最优均值的距离", "direction-aware distance from best mean", "absolute metric difference", "SUPPORTING", ["main_text", "supplement"])


def _draw_stability(axis, context: _PlotContext) -> dict[str, object]:
    scores = [1.0 / (1.0 + item.std) for item in context.items]
    axis.barh(
        range(len(scores)),
        scores,
        color=[_color(index, _series_label(item)) for index, item in enumerate(context.items)],
        alpha=0.82,
        edgecolor="white",
        linewidth=0.7,
    )
    axis.set_yticks(range(len(context.items)), [_display(_series_label(item)) for item in context.items])
    axis.invert_yaxis()
    axis.set_xlim(0, 1.02)
    axis.set_xlabel("Stability score, 1 / (1 + SD)")
    return _meta("stability_score", "Repeatability", "重复实验稳定性", "用单调变换后的标准差辅助比较可重复性；原始标准差仍保留在聚合数据中", "1 / (1 + across-seed SD)", "derived from standard deviation", "OPTIONAL", ["appendix", "supplement"])


_DRAWERS: dict[str, Callable] = {
    "raw_seed_strip": _draw_raw_strip,
    "seed_trajectory": _draw_seed_trajectory,
    "distribution_box": _draw_box,
    "empirical_cdf": _draw_ecdf,
    "rank_by_seed": _draw_rank,
    "mean_variability": _draw_mean_variability,
    "observed_range": _draw_range,
    "confidence_interval_width": _draw_ci_width,
    "successful_run_count": _draw_count,
    "paired_seed_delta": _draw_paired_delta,
    "paired_slope": _draw_paired_slope,
    "gap_to_best": _draw_gap,
    "stability_score": _draw_stability,
}


def _meta(
    visualization_type: str,
    evidence_role: str,
    title: str,
    purpose: str,
    transform: str,
    uncertainty: str,
    priority: str,
    recommended_for: list[str],
) -> dict[str, object]:
    return {
        "visualization_type": visualization_type,
        "evidence_role": evidence_role,
        "title": title,
        "purpose": purpose,
        "transform": transform,
        "uncertainty": uncertainty,
        "priority": priority,
        "recommended_for": recommended_for,
        "domain_rationale": "该图承担独立证据角色，并与均值区间主图形成互补，而不是仅更换图形样式",
        "caption": purpose + "；仅使用成功test运行产生的真实指标。",
        "limitations": ["候选图需由模块四结合全文叙事选择；不得把多个候选重复当成独立证据"],
    }


def _comparison_pair(context: _PlotContext) -> tuple[str, str] | None:
    baseline_items = [
        item for item in context.items if item.method in context.baseline_names
    ]
    primary_items = [
        item for item in context.items if item.method not in context.baseline_names
    ]
    if not baseline_items or not primary_items:
        return None
    chooser = max if context.direction == "MAXIMIZE" else min
    baseline_item = chooser(baseline_items, key=lambda item: item.mean)
    primary_item = (
        max(primary_items, key=lambda item: item.mean)
        if context.direction == "MAXIMIZE"
        else min(primary_items, key=lambda item: item.mean)
    )
    return _series_label(baseline_item), _series_label(primary_item)


def _paired_values(
    context: _PlotContext,
    left: str,
    right: str,
) -> list[tuple[int, float, float]]:
    left_values = dict(context.values.get(left, []))
    right_values = dict(context.values.get(right, []))
    return [
        (seed, left_values[seed], right_values[seed])
        for seed in sorted(set(left_values) & set(right_values))
    ]


def _ranks(context: _PlotContext, seeds: list[int]) -> list[dict[str, int]]:
    series_maps = {label: dict(context.values[label]) for label in context.series}
    rows: list[dict[str, int]] = []
    for seed in seeds:
        values = [
            (label, mapping[seed])
            for label, mapping in series_maps.items()
            if seed in mapping
        ]
        values.sort(key=lambda item: item[1], reverse=context.direction == "MAXIMIZE")
        rows.append({label: index + 1 for index, (label, _) in enumerate(values)})
    return rows


def _values_by_series(records: list[MetricRecord]) -> dict[str, list[tuple[int, float]]]:
    values: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for item in records:
        label = item.method
        if item.parameters:
            rendered = ",".join(f"{key}={value}" for key, value in sorted(item.parameters.items()))
            label = f"{item.method} ({rendered})"
        values[label].append((item.seed if item.seed is not None else -1, item.value))
    return {label: sorted(items) for label, items in values.items()}


def _all_seeds(context: _PlotContext) -> list[int]:
    return sorted({seed for values in context.values.values() for seed, _ in values})


def _offsets(count: int) -> list[float]:
    if count <= 1:
        return [0.0]
    return [-0.12 + 0.24 * index / (count - 1) for index in range(count)]


def _color(index: int, label: str | None = None) -> str:
    return method_color(label or f"series-{index}", index)


def _rotate_xticklabels(axis) -> None:
    labels = [item.get_text() for item in axis.get_xticklabels()]
    compact = [compact_label(item) for item in labels]
    if compact != labels:
        axis.set_xticklabels(compact)
    labels = compact
    compact_layout = len(labels) <= 3 and max((len(item) for item in labels), default=0) <= 22
    plt.setp(
        axis.get_xticklabels(),
        rotation=0 if compact_layout else 15,
        ha="center" if compact_layout else "right",
        rotation_mode="anchor",
    )


def _series_label(item: AggregateRecord) -> str:
    if not item.parameters:
        return item.method
    rendered = ",".join(f"{key}={value}" for key, value in sorted(item.parameters.items()))
    return f"{item.method} ({rendered})"


def _display(value: str) -> str:
    return display_label(value)


def _grid_axis(kind: str) -> str:
    if kind in {
        "observed_range",
        "confidence_interval_width",
        "gap_to_best",
        "stability_score",
        "empirical_cdf",
        "mean_variability",
    }:
        return "x"
    return "y"


def _value_limits(axis, context: _PlotContext, *, y_axis: bool = False) -> None:
    values = [value for pairs in context.values.values() for _, value in pairs]
    if not values:
        return
    low, high = min(values), max(values)
    span = high - low
    pad = span * 0.14 if span > 0 else max(abs(high) * 0.03, 0.01)
    lower, upper = low - pad, high + pad
    normalized = context.metric.casefold().replace("-", "_")
    if any(token in normalized for token in ("f1", "accuracy", "acc", "precision", "recall", "auc", "rate")):
        lower, upper = max(0.0, lower), min(1.0, upper)
    (axis.set_ylim if y_axis or axis.get_ylabel() else axis.set_xlim)(lower, upper)


def _export_paths(base_path: Path, run_dir: Path) -> dict[str, str]:
    return {
        "editable_svg": relative_to_run(base_path.with_suffix(".svg"), run_dir),
        "vector_pdf": relative_to_run(base_path.with_suffix(".pdf"), run_dir),
        "preview_png": relative_to_run(base_path.with_suffix(".png"), run_dir),
        "submission_tiff": relative_to_run(base_path.with_suffix(".tiff"), run_dir),
    }


def audit_figure_exports(base_path: Path) -> dict[str, object]:
    exports = {
        suffix.lstrip("."): base_path.with_suffix(suffix)
        for suffix in (".svg", ".pdf", ".png", ".tiff")
    }
    missing = [name for name, path in exports.items() if not path.is_file() or path.stat().st_size == 0]
    svg_text = exports["svg"].read_text(encoding="utf-8") if exports["svg"].is_file() else ""
    editable_svg = "<text" in svg_text
    minimum_pdf_font_pt: float | None = None
    pdf_audit = "NOT_AUDITED"
    text_overlap_count: int | None = None
    clipped_text_count: int | None = None
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore

        document = fitz.open(exports["pdf"])
        spans: list[tuple[int, object, dict[str, object]]] = []
        for page in document:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            spans.append((page.number, page.rect, span))
        sizes = [float(span["size"]) for _, _, span in spans]
        minimum_pdf_font_pt = min(sizes) if sizes else None
        text_overlap_count = _count_text_overlaps(fitz, spans)
        clipped_text_count = _count_clipped_text(fitz, spans)
        pdf_audit = (
            "PASS"
            if sizes
            and minimum_pdf_font_pt >= 5.0
            and text_overlap_count == 0
            and clipped_text_count == 0
            else "FAIL"
        )
        document.close()
    except (ImportError, OSError, RuntimeError, ValueError):
        pdf_audit = "REVIEW_REQUIRED"
    verdict = (
        "PASS"
        if not missing and editable_svg and pdf_audit == "PASS"
        else "REVIEW_REQUIRED"
        if not missing and editable_svg and pdf_audit == "REVIEW_REQUIRED"
        else "FAIL"
    )
    if verdict == "FAIL":
        raise ValueError(
            "candidate figure failed deterministic export QA: "
            f"missing={missing}, editable_svg={editable_svg}, pdf_audit={pdf_audit}"
        )
    return {
        "schema_version": "1.0.0",
        "verdict": verdict,
        "single_panel_alignment": "NOT_APPLICABLE",
        "editable_svg_text": editable_svg,
        "minimum_pdf_font_pt": minimum_pdf_font_pt,
        "pdf_font_audit": pdf_audit,
        "text_overlap_count": text_overlap_count,
        "clipped_text_count": clipped_text_count,
        "export_sizes": {name: path.stat().st_size for name, path in exports.items()},
        "manual_review_required": [
            "final-size salience, color accessibility and collision inspection"
        ],
    }


def _count_text_overlaps(
    fitz,
    spans: list[tuple[int, object, dict[str, object]]],
) -> int:
    count = 0
    for index, (page_number, _page_rect, left) in enumerate(spans):
        left_rect = fitz.Rect(left["bbox"])
        for other_page_number, _other_page_rect, right in spans[index + 1 :]:
            if page_number != other_page_number:
                continue
            right_rect = fitz.Rect(right["bbox"])
            intersection = left_rect & right_rect
            if intersection.is_empty:
                continue
            smaller = min(left_rect.get_area(), right_rect.get_area())
            if smaller > 0 and intersection.get_area() / smaller >= 0.05:
                count += 1
    return count


def _count_clipped_text(
    fitz,
    spans: list[tuple[int, object, dict[str, object]]],
) -> int:
    count = 0
    tolerance = 0.5
    for _page_number, page_rect, span in spans:
        rect = fitz.Rect(span["bbox"])
        if (
            rect.x0 < page_rect.x0 - tolerance
            or rect.y0 < page_rect.y0 - tolerance
            or rect.x1 > page_rect.x1 + tolerance
            or rect.y1 > page_rect.y1 + tolerance
        ):
            count += 1
    return count
