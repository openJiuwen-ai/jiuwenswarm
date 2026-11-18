# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lightweight, zero-dependency SVG chart and visualization generation tool.

Generates modern, presentation-ready vector SVG charts (bar, horizontal bar,
line, pie, donut, metric cards) without requiring external GUI or graphics
libraries (matplotlib, pillow, etc.).
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import tool

logger = logging.getLogger(__name__)

COLOR_PALETTE = (
    "#3b82f6",  # blue
    "#10b981",  # emerald
    "#f59e0b",  # amber
    "#8b5cf6",  # purple
    "#ef4444",  # rose
    "#06b6d4",  # cyan
    "#ec4899",  # pink
    "#6366f1",  # indigo
)


@dataclass(frozen=True)
class GenerateChartRequest:
    chart_type: str
    title: str
    data: dict[str, Any]
    output_path: Path
    width: int
    height: int


def _get_workspace() -> str | None:
    """Safely obtain runtime workspace path if available."""
    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_workspace

        return get_workspace()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _resolve_chart_path(raw_path: str) -> Path:
    """Resolve destination svg path against workspace."""
    cleaned = raw_path.strip().strip("'\"")
    path = Path(cleaned).expanduser()
    ws = _get_workspace()

    if ws is None:
        raise ValueError("runtime workspace is unavailable")
    ws_path = Path(ws).resolve()
    if not path.is_absolute():
        path = ws_path / path
    resolved = path.resolve()
    if not resolved.is_relative_to(ws_path):
        raise ValueError(f"Output path must be inside workspace: {raw_path}")

    if resolved.suffix.lower() != ".svg":
        resolved = resolved.with_suffix(".svg")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _escape(text: Any) -> str:
    """Escape text for safe SVG XML insertion."""
    return html.escape("" if text is None else str(text))


def _svg_header_and_grid(
    title: str, w: int, h: int, max_val: float
) -> tuple[list[str], int, int, int, int]:
    """Create common SVG header and 4-interval horizontal grid lines."""
    pad_left, pad_right, pad_top, pad_bottom = 60, 30, 70, 60
    chart_w = w - pad_left - pad_right
    chart_h = h - pad_top - pad_bottom
    elements: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" style="background:#ffffff;font-family:sans-serif;">',
        f'<text x="{w // 2}" y="40" font-size="20" font-weight="bold" '
        f'text-anchor="middle" fill="#1e293b">{_escape(title)}</text>',
    ]
    for i in range(5):
        y = pad_top + chart_h - int(i * (chart_h / 4))
        grid_val = round(max_val * (i / 4), 1)
        elements.append(
            f'<line x1="{pad_left}" y1="{y}" x2="{w - pad_right}" y2="{y}" '
            f'stroke="#e2e8f0" stroke-dasharray="4,4"/>'
        )
        elements.append(
            f'<text x="{pad_left - 10}" y="{y + 4}" font-size="11" '
            f'text-anchor="end" fill="#64748b">{grid_val}</text>'
        )
    return elements, pad_left, pad_top, chart_w, chart_h


def _render_bar_chart(req: GenerateChartRequest, labels: list[str], values: list[float]) -> str:
    """Render vertical bar chart SVG."""
    max_val = max(values) if values and max(values) > 0 else 1.0
    elements, _, _, chart_w, chart_h = _svg_header_and_grid(
        req.title, req.width, req.height, max_val
    )
    if values:
        bar_width = min(60, int(chart_w / len(values) * 0.65))
        gap = chart_w / len(values)
        for idx, (label, val) in enumerate(zip(labels, values)):
            cx = 60 + idx * gap + gap / 2
            bar_h = int((val / max_val) * chart_h) if max_val > 0 else 0
            elements.extend((
                f'<rect x="{cx - bar_width / 2:.1f}" y="{70 + chart_h - bar_h}" '
                f'width="{bar_width}" height="{bar_h}" rx="4" '
                f'fill="{COLOR_PALETTE[idx % len(COLOR_PALETTE)]}"/>',
                f'<text x="{cx:.1f}" y="{70 + chart_h - bar_h - 6}" font-size="12" '
                f'font-weight="600" text-anchor="middle" fill="#1e293b">{val}</text>',
                f'<text x="{cx:.1f}" y="{90 + chart_h}" font-size="12" '
                f'text-anchor="middle" fill="#475569">{_escape(label)}</text>',
            ))
    elements.append("</svg>")
    return "\n".join(elements)


def _render_line_chart(req: GenerateChartRequest, labels: list[str], values: list[float]) -> str:
    """Render smooth line chart SVG."""
    max_val = max(values) if values and max(values) > 0 else 1.0
    elements, _, _, chart_w, chart_h = _svg_header_and_grid(
        req.title, req.width, req.height, max_val
    )
    if len(values) == 1:
        cx = 60 + chart_w / 2
        cy = 70 + chart_h - ((values[0] / max_val) * chart_h if max_val > 0 else 0)
        elements.extend((
            f'<text x="{cx:.1f}" y="{90 + chart_h}" font-size="12" '
            f'text-anchor="middle" fill="#475569">{_escape(labels[0])}</text>',
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="#3b82f6" '
            'stroke="#ffffff" stroke-width="2"/>',
            f'<text x="{cx:.1f}" y="{cy - 10:.1f}" font-size="11" font-weight="bold" '
            f'text-anchor="middle" fill="#1e293b">{values[0]:g}</text>',
        ))
    elif len(values) > 1:
        points: list[tuple[float, float]] = []
        for idx, (label, val) in enumerate(zip(labels, values)):
            x = 60 + idx * (chart_w / (len(values) - 1))
            y = 70 + chart_h - ((val / max_val) * chart_h if max_val > 0 else 0)
            points.append((x, y))
            elements.append(
                f'<text x="{x:.1f}" y="{90 + chart_h}" font-size="12" '
                f'text-anchor="middle" fill="#475569">{_escape(label)}</text>'
            )
        elements.append(
            f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}" '
            'fill="none" stroke="#3b82f6" stroke-width="3" stroke-linecap="round"/>'
        )
        for (x, y), val in zip(points, values):
            elements.extend((
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#3b82f6" '
                'stroke="#ffffff" stroke-width="2"/>',
                f'<text x="{x:.1f}" y="{y - 10:.1f}" font-size="12" font-weight="600" '
                f'text-anchor="middle" fill="#1e293b">{val}</text>',
            ))
    elements.append("</svg>")
    return "\n".join(elements)


def _append_pie_slice(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    elements: list[str],
    cx: int,
    cy: int,
    r: int,
    start_angle: float,
    end_angle: float,
    fraction: float,
    color: str,
) -> None:
    if fraction >= 0.9999:
        elements.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
    elif fraction > 0:
        x1 = cx + r * math.cos(start_angle)
        y1 = cy + r * math.sin(start_angle)
        x2 = cx + r * math.cos(end_angle)
        y2 = cy + r * math.sin(end_angle)
        large_arc = 1 if (end_angle - start_angle) > math.pi else 0
        d = f"M {cx} {cy} L {x1:.2f} {y1:.2f} A {r} {r} 0 {large_arc} 1 {x2:.2f} {y2:.2f} Z"
        elements.append(f'<path d="{d}" fill="{color}"/>')


def _render_pie_chart(
    req: GenerateChartRequest, labels: list[str], values: list[float], *, is_donut: bool = False
) -> str:
    """Render pie or donut chart SVG with legend."""
    total = sum(values) if values else 1.0
    cx, cy = int(req.width * 0.4), int(req.height * 0.55)
    r = int(min(cx, cy) * 0.7)
    elements: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {req.width} {req.height}" '
        f'width="{req.width}" height="{req.height}" '
        'style="background:#ffffff;font-family:sans-serif;">',
        f'<text x="{req.width // 2}" y="40" font-size="20" font-weight="bold" '
        f'text-anchor="middle" fill="#1e293b">{_escape(req.title)}</text>',
    ]
    start_angle = -math.pi / 2
    for idx, (label, val) in enumerate(zip(labels, values)):
        fraction = (val / total) if total > 0 else 0
        end_angle = start_angle + fraction * 2 * math.pi
        _append_pie_slice(
            elements, cx, cy, r, start_angle, end_angle, fraction,
            COLOR_PALETTE[idx % len(COLOR_PALETTE)],
        )
        elements.extend((
            f'<rect x="{int(req.width * 0.72)}" y="{int(req.height * 0.3) + idx * 26}" '
            f'width="14" height="14" rx="3" fill="{COLOR_PALETTE[idx % len(COLOR_PALETTE)]}"/>',
            f'<text x="{int(req.width * 0.72) + 22}" y="{int(req.height * 0.3) + idx * 26 + 12}" '
            f'font-size="12" fill="#334155">{_escape(label)} ({fraction * 100:.1f}%)</text>',
        ))
        start_angle = end_angle
    if is_donut and int(r * 0.55) > 0:
        elements.append(f'<circle cx="{cx}" cy="{cy}" r="{int(r * 0.55)}" fill="#ffffff"/>')
    elements.append("</svg>")
    return "\n".join(elements)


def _render_metric_card(title: str, data: dict[str, Any], w: int, h: int) -> str:
    """Render KPI metric dashboard card SVG."""
    val = _escape(data.get("value", ""))
    subtext = _escape(data.get("subtext", ""))

    delta_elem = ""
    if "delta" in data and data["delta"] is not None:
        delta_str = str(data["delta"])
        delta_color = "#10b981" if delta_str.startswith("+") else "#ef4444"
        delta_elem = (
            f'<text x="{w // 2}" y="170" font-size="16" font-weight="600" '
            f'text-anchor="middle" fill="{delta_color}">{_escape(delta_str)}</text>'
        )

    subtext_elem = ""
    if subtext:
        subtext_elem = (
            f'<text x="{w // 2}" y="200" font-size="13" '
            f'text-anchor="middle" fill="#64748b">{subtext}</text>'
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"
width="{w}" height="{h}" style="background:#ffffff;font-family:sans-serif;">
  <rect x="20" y="20" width="{w - 40}" height="{h - 40}" rx="16"
        fill="#f8fafc" stroke="#e2e8f0" stroke-width="2"/>
  <text x="{w // 2}" y="65" font-size="18" font-weight="500"
        text-anchor="middle" fill="#475569">{_escape(title)}</text>
  <text x="{w // 2}" y="130" font-size="44" font-weight="bold"
        text-anchor="middle" fill="#0f172a">{val}</text>
  {delta_elem}
  {subtext_elem}
</svg>"""


def _validate_series_data(
    chart_type: str, data: dict[str, Any]
) -> tuple[list[str], list[float], str | None]:
    labels_raw = data.get("labels", [])
    values_raw = data.get("values", [])
    if not isinstance(labels_raw, list) or not isinstance(values_raw, list):
        return [], [], "[ERROR]: Chart data must contain 'labels' and 'values' lists."
    if not labels_raw or not values_raw or len(labels_raw) != len(values_raw):
        return [], [], (
            "[ERROR]: Chart data must contain non-empty, equal-length 'labels' "
            f"and 'values' arrays. Got {len(labels_raw)} labels and {len(values_raw)} values."
        )
    labels = [str(x) for x in labels_raw]
    try:
        values = [float(x) for x in values_raw]
    except (ValueError, TypeError) as exc:
        return [], [], f"[ERROR]: All entries in 'values' must be numeric: {exc}"
    if not all(math.isfinite(v) for v in values):
        return [], [], "[ERROR]: values must be finite numbers."
    if any(v < 0 for v in values):
        return [], [], "[ERROR]: All entries in 'values' must be non-negative (>= 0)."
    if chart_type in ("pie", "donut") and sum(values) <= 0:
        return [], [], "[ERROR]: Total sum of values must be greater than 0 for pie/donut charts."
    return labels, values, None


def _render_series_chart(
    chart_type: str, req: GenerateChartRequest
) -> tuple[str | None, str | None]:
    """Validate series data and return (svg_content, error_message)."""
    labels, values, err = _validate_series_data(chart_type, req.data)
    if err is not None:
        return None, err
    if chart_type in ("bar", "column"):
        return _render_bar_chart(req, labels, values), None
    if chart_type == "line":
        return _render_line_chart(req, labels, values), None
    if chart_type in ("pie", "donut"):
        return _render_pie_chart(req, labels, values, is_donut=(chart_type == "donut")), None
    return None, (
        f"[ERROR]: Unsupported chart_type '{chart_type}'. "
        "Supported types: 'bar', 'line', 'pie', 'donut', 'metric_card'."
    )


def _generate_chart_sync(req: GenerateChartRequest) -> str:
    """Generate SVG chart and save to file synchronously."""
    chart_type = req.chart_type.strip().lower()
    if chart_type == "metric_card":
        if "value" not in req.data or req.data["value"] is None:
            return "[ERROR]: Metric card data must contain a 'value' field."
        svg_content = _render_metric_card(req.title, req.data, req.width, req.height)
    else:
        rendered, err = _render_series_chart(chart_type, req)
        if err is not None or rendered is None:
            return err or "[ERROR]: Failed to render chart."
        svg_content = rendered

    try:
        req.output_path.write_text(svg_content, encoding="utf-8")
    except OSError as exc:
        return f"[ERROR]: Failed to write SVG file to {req.output_path}: {exc}"

    ws = _get_workspace()
    ws_base = Path(ws).resolve() if ws is not None else Path.cwd().resolve()
    try:
        rel_str = str(req.output_path.resolve().relative_to(ws_base))
    except ValueError:
        rel_str = req.output_path.name

    return (
        f"Successfully generated {chart_type} chart '{req.title}' "
        f"at `{rel_str}` ({req.width}x{req.height}).\n"
        f"![{req.title}]({rel_str})"
    )


@tool(
    name="generate_chart",
    description=(
        "Generate lightweight, zero-dependency presentation vector SVG charts "
        "(bar, line, pie, donut, metric_card) from structured data. "
        "Saves the rendered SVG into the workspace and outputs image preview markdown."
    ),
)
async def generate_chart(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    chart_type: str,
    title: str,
    data: dict[str, Any],
    output_path: str = "chart.svg",
    width: int = 700,
    height: int = 420,
) -> str:
    """Async entrypoint for vector SVG chart generation."""
    logger.info(
        "[generate_chart] type=%s title=%s output=%s",
        chart_type,
        title,
        output_path,
    )
    if not title:
        return "[ERROR]: Title cannot be empty."

    try:
        dest_path = _resolve_chart_path(output_path)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: Invalid output path: {exc}"

    capped_w = min(max(300, width), 2000)
    capped_h = min(max(200, height), 2000)

    req = GenerateChartRequest(
        chart_type=chart_type,
        title=title,
        data=data,
        output_path=dest_path,
        width=capped_w,
        height=capped_h,
    )
    try:
        return await asyncio.to_thread(_generate_chart_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: Failed to generate chart: {exc}"
