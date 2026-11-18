# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for SVG chart generation tool (generate_chart)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.chart_tools import (
    _escape,
    _resolve_chart_path,
    generate_chart,
)


@pytest.fixture(autouse=True)
def _mock_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.cwd.get_workspace",
        lambda: str(tmp_path),
    )


def test_escape() -> None:
    """Test HTML/XML character escaping."""
    assert _escape("Sales & Revenue <Q1>") == "Sales &amp; Revenue &lt;Q1&gt;"
    assert _escape("") == ""


def test_resolve_chart_path(tmp_path: Path) -> None:
    """Test destination path resolution and .svg suffix enforcement."""
    path = _resolve_chart_path(str(tmp_path / "charts" / "report"))
    assert path.suffix == ".svg"
    assert path.parent.exists()


@pytest.mark.asyncio
async def test_generate_bar_chart(tmp_path: Path) -> None:
    """Test generating vertical bar chart."""
    out_file = tmp_path / "bar.svg"
    data = {
        "labels": ["Q1", "Q2", "Q3", "Q4"],
        "values": [120, 250, 180, 310],
    }

    result = await generate_chart.invoke({
        "chart_type": "bar",
        "title": "Quarterly Sales",
        "data": data,
        "output_path": str(out_file),
    })

    assert "Successfully generated bar chart" in result
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "<svg" in content
    assert "Quarterly Sales" in content
    assert "Q1" in content
    assert "310" in content


@pytest.mark.asyncio
async def test_generate_line_chart(tmp_path: Path) -> None:
    """Test generating line chart with points."""
    out_file = tmp_path / "line.svg"
    data = {
        "labels": ["Jan", "Feb", "Mar"],
        "values": [10.5, 22.0, 18.2],
    }

    result = await generate_chart.invoke({
        "chart_type": "line",
        "title": "Monthly Performance",
        "data": data,
        "output_path": str(out_file),
    })

    assert "Successfully generated line chart" in result
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "<polyline" in content
    assert "Monthly Performance" in content


@pytest.mark.asyncio
async def test_generate_pie_and_donut_chart(tmp_path: Path) -> None:
    """Test generating pie and donut charts."""
    pie_file = tmp_path / "pie.svg"
    donut_file = tmp_path / "donut.svg"
    data = {
        "labels": ["Chrome", "Safari", "Edge"],
        "values": [65, 20, 15],
    }

    res_pie = await generate_chart.invoke({
        "chart_type": "pie",
        "title": "Browser Share",
        "data": data,
        "output_path": str(pie_file),
    })
    assert "Successfully generated pie chart" in res_pie
    assert pie_file.exists()

    res_donut = await generate_chart.invoke({
        "chart_type": "donut",
        "title": "Browser Share Donut",
        "data": data,
        "output_path": str(donut_file),
    })
    assert "Successfully generated donut chart" in res_donut
    assert donut_file.exists()


@pytest.mark.asyncio
async def test_generate_metric_card(tmp_path: Path) -> None:
    """Test generating KPI metric card."""
    out_file = tmp_path / "kpi.svg"
    data = {
        "value": "99.98%",
        "delta": "+0.45%",
        "subtext": "vs target 99.5%",
    }

    result = await generate_chart.invoke({
        "chart_type": "metric_card",
        "title": "API Availability",
        "data": data,
        "output_path": str(out_file),
    })

    assert "Successfully generated metric_card chart" in result
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "99.98%" in content
    assert "+0.45%" in content
    assert "API Availability" in content


@pytest.mark.asyncio
async def test_generate_chart_mismatched_data(tmp_path: Path) -> None:
    """Test error handling when labels and values count mismatch."""
    result = await generate_chart.invoke({
        "chart_type": "bar",
        "title": "Invalid Data",
        "data": {"labels": ["A", "B"], "values": [1]},
        "output_path": str(tmp_path / "err.svg"),
    })
    assert "[ERROR]" in result
    assert "equal-length" in result


@pytest.mark.asyncio
async def test_generate_chart_non_numeric_values(tmp_path: Path) -> None:
    """Test error handling when values contain non-numbers."""
    result = await generate_chart.invoke({
        "chart_type": "bar",
        "title": "Invalid Types",
        "data": {"labels": ["A"], "values": ["not_a_number"]},
        "output_path": str(tmp_path / "err.svg"),
    })
    assert "[ERROR]" in result
    assert "numeric" in result


@pytest.mark.asyncio
async def test_generate_chart_unsupported_type(tmp_path: Path) -> None:
    """Test unsupported chart type error."""
    result = await generate_chart.invoke({
        "chart_type": "radar",
        "title": "Unsupported",
        "data": {"labels": ["A"], "values": [1]},
        "output_path": str(tmp_path / "err.svg"),
    })
    assert "[ERROR]" in result
    assert "Unsupported chart_type" in result


@pytest.mark.asyncio
async def test_generate_chart_empty_title(tmp_path: Path) -> None:
    """Test empty title validation."""
    result = await generate_chart.invoke({
        "chart_type": "bar",
        "title": "",
        "data": {"labels": ["A"], "values": [1]},
        "output_path": str(tmp_path / "err.svg"),
    })
    assert "[ERROR]" in result
    assert "Title cannot be empty" in result


@pytest.mark.asyncio
async def test_generate_chart_empty_dataset(tmp_path: Path) -> None:
    """Test error handling when dataset is completely empty."""
    target_file = tmp_path / "empty.svg"
    result = await generate_chart.invoke({
        "chart_type": "bar",
        "title": "Empty Data",
        "data": {"labels": [], "values": []},
        "output_path": str(target_file),
    })
    assert "[ERROR]" in result
    assert "non-empty" in result
    assert not target_file.exists()


@pytest.mark.asyncio
async def test_generate_chart_negative_values(tmp_path: Path) -> None:
    """Test error handling when values contain negative numbers."""
    target_file = tmp_path / "negative.svg"
    result = await generate_chart.invoke({
        "chart_type": "bar",
        "title": "Negative Numbers",
        "data": {"labels": ["Loss", "Gain"], "values": [-50, 100]},
        "output_path": str(target_file),
    })
    assert "[ERROR]" in result
    assert "non-negative" in result
    assert not target_file.exists()


@pytest.mark.asyncio
async def test_generate_pie_all_zeros(tmp_path: Path) -> None:
    """Test error handling when pie chart values sum to zero."""
    target_file = tmp_path / "all_zeros.svg"
    result = await generate_chart.invoke({
        "chart_type": "pie",
        "title": "Zero Sum",
        "data": {"labels": ["A", "B"], "values": [0, 0]},
        "output_path": str(target_file),
    })
    assert "[ERROR]" in result
    assert "greater than 0" in result
    assert not target_file.exists()


@pytest.mark.asyncio
async def test_generate_metric_card_missing_value(tmp_path: Path) -> None:
    """Test metric card validation when 'value' is missing."""
    target_file = tmp_path / "missing_val.svg"
    result = await generate_chart.invoke({
        "chart_type": "metric_card",
        "title": "Missing Value",
        "data": {"subtext": "No value field"},
        "output_path": str(target_file),
    })
    assert "[ERROR]" in result
    assert "'value' field" in result
    assert not target_file.exists()


@pytest.mark.asyncio
async def test_generate_metric_card_zero_value(tmp_path: Path) -> None:
    """Test metric card rendering when value is numeric zero (0)."""
    target_file = tmp_path / "zero_val.svg"
    result = await generate_chart.invoke({
        "chart_type": "metric_card",
        "title": "Zero Count",
        "data": {"value": 0, "delta": 0, "subtext": "No errors"},
        "output_path": str(target_file),
    })
    assert "Successfully generated" in result
    assert target_file.exists()
    content = target_file.read_text(encoding="utf-8")
    assert ">0<" in content
    assert "Zero Count" in content


@pytest.mark.asyncio
async def test_generate_chart_workspace_escape_rejected(tmp_path: Path) -> None:
    """Test rejection when output path tries to escape workspace."""
    # pylint: disable-next=import-outside-toplevel
    from unittest.mock import patch

    with patch(
        "openjiuwen.core.sys_operation.cwd.get_workspace",
        return_value=str(tmp_path),
    ):
        result = await generate_chart.invoke({
            "chart_type": "bar",
            "title": "Escape Test",
            "data": {"labels": ["A"], "values": [10]},
            "output_path": "../../escaped_chart.svg",
        })
    assert "[ERROR]" in result
    assert "must be inside workspace" in result


