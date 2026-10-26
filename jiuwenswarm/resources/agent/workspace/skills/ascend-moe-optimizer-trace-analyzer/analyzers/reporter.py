from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional


@dataclass
class StatisticalSummaryInput:
    overview: dict
    phase_summary: List[dict]
    category_summary: List[dict]
    core_group_summary: List[dict]
    category_core_group_summary: List[dict]
    overlap_summary: List[dict]
    bubble_summary: List[dict]
    heading: str = "## Statistical Highlights"


@dataclass
class MarkdownReportInput:
    overview: dict
    phase_summary: List[dict]
    category_summary: List[dict]
    core_group_summary: List[dict]
    phase_core_group_summary: List[dict]
    category_core_group_summary: List[dict]
    name_summary: List[dict]
    overlap_summary: List[dict]
    bubble_summary: List[dict]
    diagnosis: dict
    llm_analysis: Optional[dict] = None
    plot_files: Optional[List[str]] = None
    statistical_summary: Optional[str] = None
    top_n: int = 20


_CORE_GROUP_TABLE_COLS = [
    "core_group",
    "core_kind",
    "core_type",
    "observed_core_count",
    "count",
    "union_us",
    "ratio_to_total_wall",
    "total_us",
]

_BUBBLE_TABLE_COLS = [
    "parent_phase",
    "parent_union_us",
    "child_covered_us",
    "bubble_us",
    "bubble_ratio",
    "gap_count",
    "max_gap_us",
]


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _collect_fieldnames(rows: List[dict], preferred: Optional[Iterable[str]] = None) -> List[str]:
    fieldnames: List[str] = []
    for name in preferred or []:
        if name not in fieldnames:
            fieldnames.append(name)

    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    return fieldnames


def save_table(rows: List[dict], path: str, fieldnames: Optional[Iterable[str]] = None) -> None:
    columns = _collect_fieldnames(rows, fieldnames)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def save_dataframe(rows: List[dict], path: str) -> None:
    # Backward-compatible name used by app.py.
    save_table(rows, path)


def save_json(data: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, float):
        if abs(value) >= 100:
            return f"{value:.3f}".rstrip("0").rstrip(".")
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt_us(value: Any) -> str:
    return f"{_as_float(value):.3f} us"


def _fmt_pct(value: Any) -> str:
    return f"{_as_float(value) * 100:.1f}%"


def _top_rows(rows: List[dict], key: str, limit: int = 3) -> List[dict]:
    return sorted(rows, key=lambda row: _as_float(row.get(key)), reverse=True)[:limit]


def _join_row_summaries(rows: List[dict], label_key: str, value_key: str, ratio_key: str) -> str:
    parts = []
    for row in rows:
        parts.append(
            f"{row.get(label_key)}={_fmt_us(row.get(value_key))} ({_fmt_pct(row.get(ratio_key))})"
        )
    return ", ".join(parts)


def _join_share_summaries(rows: List[dict], label_key: str, value_key: str) -> str:
    total = sum(_as_float(row.get(value_key)) for row in rows)
    if total <= 0:
        return ""
    parts = []
    for row in rows:
        share = _as_float(row.get(value_key)) / total
        parts.append(f"{row.get(label_key)}={_fmt_us(row.get(value_key))} ({share * 100:.1f}%)")
    return ", ".join(parts)


def _find_overlap_pair(rows: List[dict], phase_a: str, phase_b: str) -> Optional[dict]:
    target = {phase_a, phase_b}
    for row in rows:
        if {row.get("phase_a"), row.get("phase_b")} == target:
            return row
    return None


def build_statistical_summary(inp: StatisticalSummaryInput) -> str:
    overview = inp.overview
    phase_summary = inp.phase_summary
    category_summary = inp.category_summary
    core_group_summary = inp.core_group_summary
    category_core_group_summary = inp.category_core_group_summary
    heading = inp.heading
    lines: List[str] = [heading, ""]

    core_groups = overview.get("core_groups") or []
    lines.append(
        "- Trace scope: "
        f"{overview.get('num_instances', 0)} mapped intervals, "
        f"{overview.get('num_phases', 0)} phases, "
        f"{overview.get('num_tids', 0)} tids, "
        f"core groups={', '.join(str(group) for group in core_groups)}, "
        f"wall={_fmt_us(overview.get('total_wall_us'))}."
    )

    if core_group_summary:
        top_core_groups = _top_rows(core_group_summary, "union_us", 3)
        lines.append(
            "- Core group wall coverage: "
            + _join_row_summaries(top_core_groups, "core_group", "union_us", "ratio_to_total_wall")
            + "."
        )

    actionable_categories = [
        row for row in category_summary
        if row.get("category") not in {None, "", "container"}
    ]
    if actionable_categories:
        top_categories = _top_rows(actionable_categories, "union_us", 4)
        lines.append(
            "- Top non-container categories by wall coverage: "
            + _join_row_summaries(top_categories, "category", "union_us", "ratio_to_total_wall")
            + "."
        )
        event_time_rows = _top_rows(actionable_categories, "total_us", 8)
        lines.append(
            "- Category pie basis: non-container category event-time share uses `total_us`, so slices sum to 100%. "
            + _join_share_summaries(event_time_rows, "category", "total_us")
            + "."
        )

    if category_core_group_summary:
        per_group = []
        for core_group in core_groups:
            group_rows = []
            for row in category_core_group_summary:
                if row.get("core_group") != core_group:
                    continue
                if row.get("category") in {None, "", "container"}:
                    continue
                group_rows.append(row)
            if not group_rows:
                continue
            top_row = _top_rows(group_rows, "union_us", 1)[0]
            per_group.append(
                f"{core_group}: {top_row.get('category')} "
                f"{_fmt_us(top_row.get('union_us'))} "
                f"({_fmt_pct(top_row.get('ratio_to_core_group_wall'))} of group)"
            )
        if per_group:
            lines.append("- Leading category per core group: " + "; ".join(per_group) + ".")

    actionable_phases = [
        row for row in phase_summary
        if row.get("category") != "container"
    ]
    if actionable_phases:
        top_phases = _top_rows(actionable_phases, "union_us", 5)
        phase_parts = [
            f"{row.get('phase')}={_fmt_us(row.get('union_us'))} "
            f"({_fmt_pct(row.get('ratio_to_total_wall'))}, {row.get('category')})"
            for row in top_phases
        ]
        lines.append("- Top non-container phases: " + ", ".join(phase_parts) + ".")

    lines.append("")
    return "\n".join(lines)


def _markdown_table(rows: List[dict], columns: List[str], limit: Optional[int] = None) -> str:
    if not rows:
        return "No data."

    selected = rows[:limit] if limit else rows
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(_format_cell(row.get(column)) for column in columns) + " |"
        for row in selected
    ]
    return "\n".join([header, sep] + body)


def _diagnosis_lines(diagnosis: dict) -> List[str]:
    lines: List[str] = []
    headline = diagnosis.get("headline")
    if headline:
        lines.append(headline)
        lines.append("")

    findings = diagnosis.get("findings", [])
    if not findings:
        lines.append("No automatic findings.")
        return lines

    for idx, finding in enumerate(findings, 1):
        title = finding.get("title", "Finding")
        severity = finding.get("severity", "info")
        lines.append(f"{idx}. [{severity}] {title}")
        detail = finding.get("detail")
        if detail:
            lines.append(f"   Evidence: {detail}")
        recommendation = finding.get("recommendation")
        if recommendation:
            lines.append(f"   Action: {recommendation}")
    return lines


def _sort_low_overlap_rows(overlap_summary: List[dict]) -> List[dict]:
    candidates: List[dict] = []
    for row in overlap_summary:
        if row.get("overlap_ratio_min", 0.0) > 0.2:
            continue
        ua = row.get("union_us_a", 0.0)
        ub = row.get("union_us_b", 0.0)
        if min(ua, ub) <= 0:
            continue
        candidates.append(row)
    candidates.sort(
        key=lambda r: (
            min(r.get("union_us_a", 0.0), r.get("union_us_b", 0.0)),
            -r.get("overlap_us", 0.0),
        ),
        reverse=True,
    )
    return candidates


def build_markdown_report(inp: MarkdownReportInput) -> str:
    overview = inp.overview
    phase_summary = inp.phase_summary
    category_summary = inp.category_summary
    core_group_summary = inp.core_group_summary
    phase_core_group_summary = inp.phase_core_group_summary
    category_core_group_summary = inp.category_core_group_summary
    name_summary = inp.name_summary
    overlap_summary = inp.overlap_summary
    bubble_summary = inp.bubble_summary
    diagnosis = inp.diagnosis
    llm_analysis = inp.llm_analysis
    plot_files = inp.plot_files
    statistical_summary = inp.statistical_summary
    top_n = inp.top_n
    lines: List[str] = []
    lines.append("# Ascend MoE Trace Report")
    lines.append("")

    lines.append("## Overview")
    lines.append("")
    lines.append(
        _markdown_table(
            [overview],
            [
                "num_instances",
                "num_phases",
                "num_names",
                "num_pids",
                "num_tids",
                "num_core_groups",
                "core_groups",
                "total_wall_us",
            ],
        )
    )
    lines.append("")

    if plot_files:
        lines.append("## Visualizations")
        lines.append("")
        for plot_file in plot_files:
            title = os.path.splitext(os.path.basename(plot_file))[0].replace("_", " ").title()
            lines.append(f"![{title}]({plot_file})")
            lines.append("")

    if statistical_summary:
        lines.append(statistical_summary.strip())
        lines.append("")

    lines.append("## Automatic Diagnosis")
    lines.append("")
    lines.extend(_diagnosis_lines(diagnosis))
    lines.append("")

    if llm_analysis and llm_analysis.get("enabled"):
        lines.append("## LLM Analysis")
        lines.append("")
        if llm_analysis.get("analysis"):
            lines.append(llm_analysis["analysis"])
        else:
            lines.append(
                "LLM analysis was requested, but no analysis text was produced. "
                "See `llm_prompt.md` and `llm_analysis_meta.json` in the output directory."
            )
        lines.append("")

    lines.append("## Core Group Summary")
    lines.append("")
    lines.append(
        _markdown_table(
            core_group_summary,
            _CORE_GROUP_TABLE_COLS,
            top_n,
        )
    )
    lines.append("")

    lines.append("## Category By Core Group")
    lines.append("")
    lines.append(
        _markdown_table(
            category_core_group_summary,
            [
                "core_group",
                "category",
                "observed_core_count",
                "count",
                "union_us",
                "ratio_to_core_group_wall",
                "ratio_to_total_wall",
                "total_us",
            ],
            top_n * 3,
        )
    )
    lines.append("")

    lines.append("## Phase By Core Group")
    lines.append("")
    lines.append(
        _markdown_table(
            phase_core_group_summary,
            [
                "core_group",
                "phase",
                "category",
                "observed_core_count",
                "count",
                "union_us",
                "ratio_to_core_group_wall",
                "total_us",
            ],
            top_n * 3,
        )
    )
    lines.append("")

    lines.append("## Category Summary")
    lines.append("")
    lines.append(
        _markdown_table(
            category_summary,
            ["category", "count", "union_us", "ratio_to_total_wall", "total_us", "tid_nunique"],
            top_n,
        )
    )
    lines.append("")

    lines.append("## Phase Summary")
    lines.append("")
    lines.append(
        _markdown_table(
            phase_summary,
            ["phase", "category", "count", "union_us", "ratio_to_total_wall", "total_us", "tid_nunique"],
            top_n,
        )
    )
    lines.append("")

    lines.append("## Bubble Summary")
    lines.append("")
    lines.append(
        _markdown_table(
            bubble_summary,
            _BUBBLE_TABLE_COLS,
            top_n,
        )
    )
    lines.append("")

    lines.append("## Top Raw Names")
    lines.append("")
    lines.append(
        _markdown_table(
            name_summary,
            ["name", "phase", "category", "count", "total_us", "union_us", "tid_nunique"],
            top_n,
        )
    )
    lines.append("")

    lines.append("## High Overlap Pairs")
    lines.append("")
    lines.append(
        _markdown_table(
            overlap_summary,
            ["phase_a", "phase_b", "overlap_us", "overlap_ratio_a", "overlap_ratio_b", "union_us_a", "union_us_b"],
            top_n,
        )
    )
    lines.append("")

    low_overlap = _sort_low_overlap_rows(overlap_summary)
    lines.append("## Low Overlap Pairs")
    lines.append("")
    lines.append(
        _markdown_table(
            low_overlap,
            ["phase_a", "phase_b", "overlap_us", "overlap_ratio_min", "union_us_a", "union_us_b"],
            top_n,
        )
    )
    lines.append("")

    return "\n".join(lines)


def save_text(text: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
