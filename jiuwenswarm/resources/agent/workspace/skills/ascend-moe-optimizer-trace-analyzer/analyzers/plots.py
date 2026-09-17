from __future__ import annotations

import os
import tempfile
from typing import Any, List


_CATEGORY_ORDER = [
    "container",
    "wait",
    "sync",
    "compute",
    "epilogue",
    "communication",
    "quant",
    "init",
    "cleanup",
    "other",
]
_CORE_GROUP_ORDER = ["cube", "vector_recv", "vector_send", "unknown"]
_CATEGORY_COLORS = {
    "container": "#8a8f98",
    "wait": "#d64f45",
    "sync": "#c58a2c",
    "compute": "#2f7ebc",
    "epilogue": "#7b61b3",
    "communication": "#25966f",
    "quant": "#b85c9e",
    "init": "#6b9f3f",
    "cleanup": "#6d6d6d",
    "other": "#9a9a9a",
}


def _load_pyplot():
    os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "trace_analysis_matplotlib"))
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    return plt


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _short_label(value: Any, width: int = 42) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _show_no_data(ax: Any, title: str) -> None:
    ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(title)
    ax.set_axis_off()


def _draw_core_group(ax: Any, rows: List[dict]) -> None:
    title = "Core Group Wall Coverage"
    if not rows:
        _show_no_data(ax, title)
        return

    ordered = sorted(
        rows,
        key=lambda row: (
            _CORE_GROUP_ORDER.index(row.get("core_group"))
            if row.get("core_group") in _CORE_GROUP_ORDER
            else 99,
            -_float(row.get("union_us")),
        ),
    )
    labels = [str(row.get("core_group")) for row in ordered]
    values = [_float(row.get("union_us")) for row in ordered]
    ratios = [_float(row.get("ratio_to_total_wall")) for row in ordered]

    bars = ax.barh(labels, values, color=["#5e8fd6", "#4aa877", "#d48750", "#999999"][: len(labels)])
    ax.invert_yaxis()
    ax.set_xlabel("Union time (us)")
    ax.set_title(title)
    ax.grid(True, axis="x", linestyle="--", alpha=0.35)
    for bar, ratio in zip(bars, ratios):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f" {ratio * 100:.1f}%", va="center", fontsize=8)


def _draw_category_pie(ax: Any, rows: List[dict]) -> None:
    title = "Non-container Category Event-Time Share\nUses total_us; slices sum to 100%"
    if not rows:
        _show_no_data(ax, title)
        return

    selected: List[dict] = []
    for row in rows:
        if row.get("category") in {None, "", "container"}:
            continue
        if _float(row.get("total_us")) <= 0:
            continue
        selected.append(row)
    if not selected:
        _show_no_data(ax, title)
        return

    selected = sorted(selected, key=lambda row: _float(row.get("total_us")), reverse=True)
    labels = [str(row.get("category")) for row in selected]
    values = [_float(row.get("total_us")) for row in selected]
    colors = [_CATEGORY_COLORS.get(label, _CATEGORY_COLORS["other"]) for label in labels]

    wedges, _, autotexts = ax.pie(
        values,
        colors=colors,
        startangle=90,
        counterclock=False,
        autopct=lambda pct: f"{pct:.1f}%" if pct >= 2.0 else "",
        pctdistance=0.75,
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
    )
    ax.set_title(title)
    for text in autotexts:
        text.set_fontsize(8)
        text.set_color("#222222")
    ax.legend(wedges, labels, loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8)


def _draw_top_phases(ax: Any, rows: List[dict], top_n: int) -> None:
    title = "Top Phases By Union Time"
    if not rows:
        _show_no_data(ax, title)
        return

    selected = rows[:top_n]
    labels = [_short_label(row.get("phase")) for row in selected]
    values = [_float(row.get("union_us")) for row in selected]
    categories = [str(row.get("category") or "other") for row in selected]
    colors = [_CATEGORY_COLORS.get(category, _CATEGORY_COLORS["other"]) for category in categories]

    ax.barh(labels, values, color=colors)
    ax.invert_yaxis()
    ax.set_xlabel("Union time (us)")
    ax.set_title(title)
    ax.grid(True, axis="x", linestyle="--", alpha=0.35)


def plot_analysis_charts(
    output_path: str,
    phase_summary: List[dict],
    category_summary: List[dict],
    core_group_summary: List[dict],
    top_n: int = 12,
) -> str:
    plt = _load_pyplot()
    fig = plt.figure(figsize=(16, 10))
    grid = fig.add_gridspec(2, 2, height_ratios=[0.9, 1.2])

    _draw_core_group(fig.add_subplot(grid[0, 0]), core_group_summary)
    _draw_category_pie(fig.add_subplot(grid[0, 1]), category_summary)
    _draw_top_phases(fig.add_subplot(grid[1, :]), phase_summary, top_n=min(top_n, 12))

    fig.suptitle("Trace Statistical Overview", fontsize=16, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.98])
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return output_path


def generate_summary_plots(
    output_dir: str,
    phase_summary: List[dict],
    category_summary: List[dict],
    core_group_summary: List[dict],
    top_n: int = 12,
) -> List[str]:
    os.makedirs(output_dir, exist_ok=True)
    filename = "analysis_charts.png"
    plot_analysis_charts(
        output_path=os.path.join(output_dir, filename),
        phase_summary=phase_summary,
        category_summary=category_summary,
        core_group_summary=core_group_summary,
        top_n=top_n,
    )
    return [filename]
