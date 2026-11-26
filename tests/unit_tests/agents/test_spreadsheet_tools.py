# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for structured spreadsheet reader (read_spreadsheet)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

from jiuwenswarm.agents.harness.common.tools.spreadsheet_tools import (
    _build_markdown_table,
    _format_cell_value,
    read_spreadsheet,
)


def _create_sample_csv(tmp_path: Path) -> Path:
    """Create a sample UTF-8 CSV file."""
    csv_file = tmp_path / "sales.csv"
    csv_file.write_text(
        "Product,Q1,Q2\nWidget A,100,150\nWidget B,200,250\n",
        encoding="utf-8",
    )
    return csv_file


def _create_sample_gbk_csv(tmp_path: Path) -> Path:
    """Create a sample GBK CSV file."""
    csv_file = tmp_path / "sales_gbk.csv"
    csv_file.write_text(
        "商品,第一季度\n商品A,100\n",
        encoding="gbk",
    )
    return csv_file


def _create_sample_excel(tmp_path: Path) -> Path:
    """Create a multi-sheet Excel (.xlsx) file."""
    excel_file = tmp_path / "company_data.xlsx"
    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "Employees"
    ws1.append(["ID", "Name", "Department"])
    ws1.append([1, "Alice", "R&D"])
    ws1.append([2, "Bob", "Product"])

    ws2 = wb.create_sheet(title="Finances")
    ws2.append(["Quarter", "Revenue", "Cost"])
    ws2.append(["2025Q1", "$10,000", "$6,000"])

    wb.save(str(excel_file))
    return excel_file


def test_format_cell_value() -> None:
    """Verify cell value formatting and cleaning."""
    assert _format_cell_value(None) == ""
    assert _format_cell_value("  hello\nworld  ") == "hello world"
    assert _format_cell_value("A|B|C") == "A\\|B\\|C"
    long_text = "a" * 200
    formatted = _format_cell_value(long_text)
    assert len(formatted) == 123
    assert formatted.endswith("...")


def test_build_markdown_table() -> None:
    """Verify Markdown table construction."""
    assert _build_markdown_table([]) == "[Empty table]"
    assert _build_markdown_table([[]]) == "[Empty table]"
    rows = [["Col A", "Col B"], ["1", "2"]]
    table = _build_markdown_table(rows)
    assert "| Col A | Col B |" in table
    assert "| --- | --- |" in table
    assert "| 1 | 2 |" in table


@pytest.mark.asyncio
async def test_read_csv_utf8(tmp_path: Path) -> None:
    """Verify UTF-8 CSV parsing and Markdown table output."""
    csv_path = _create_sample_csv(tmp_path)
    result = await read_spreadsheet.invoke({"file_path": str(csv_path)})
    assert "**File: sales.csv (CSV, Showing rows 1-2 of 2)**" in result
    assert "| Product | Q1 | Q2 |" in result
    assert "| --- | --- | --- |" in result
    assert "| Widget A | 100 | 150 |" in result
    assert "| Widget B | 200 | 250 |" in result


@pytest.mark.asyncio
async def test_read_csv_gbk_encoding(tmp_path: Path) -> None:
    """Verify automatic encoding fallback for GBK CSV files."""
    csv_path = _create_sample_gbk_csv(tmp_path)
    result = await read_spreadsheet.invoke({"file_path": str(csv_path)})
    assert "商品" in result
    assert "第一季度" in result
    assert "商品A" in result


@pytest.mark.asyncio
async def test_read_csv_empty(tmp_path: Path) -> None:
    """Verify empty or zero-byte CSV returns friendly empty message."""
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_bytes(b"")
    res_zero = await read_spreadsheet.invoke({"file_path": str(empty_csv)})
    assert "[CSV file is empty]" in res_zero

    bom_csv = tmp_path / "bom_empty.csv"
    bom_csv.write_bytes(b"\xef\xbb\xbf")
    res_bom = await read_spreadsheet.invoke({"file_path": str(bom_csv)})
    assert "[CSV file is empty]" in res_bom


@pytest.mark.asyncio
async def test_read_csv_pagination(tmp_path: Path) -> None:
    """Verify pagination via start_row and max_rows."""
    csv_file = tmp_path / "large.csv"
    lines = ["ID,Value"] + [f"{i},Val_{i}" for i in range(1, 101)]
    csv_file.write_text("\n".join(lines), encoding="utf-8")

    res_p1 = await read_spreadsheet.invoke({
        "file_path": str(csv_file),
        "start_row": 1,
        "max_rows": 10,
    })
    assert "Showing rows 1-10 of 100" in res_p1
    assert "start_row=11" in res_p1
    assert "| 1 | Val_1 |" in res_p1
    assert "| 11 | Val_11 |" not in res_p1

    res_p2 = await read_spreadsheet.invoke({
        "file_path": str(csv_file),
        "start_row": 11,
        "max_rows": 10,
    })
    assert "Showing rows 11-20 of 100" in res_p2
    assert "| 11 | Val_11 |" in res_p2
    assert "| 1 | Val_1 |" not in res_p2


@pytest.mark.asyncio
async def test_read_excel_default_sheet(tmp_path: Path) -> None:
    """Verify default sheet reading and navigation hints."""
    excel_path = _create_sample_excel(tmp_path)
    result = await read_spreadsheet.invoke({"file_path": str(excel_path)})
    assert "Sheet: Employees" in result
    assert "**[Employees]** (active)" in result
    assert "Finances" in result
    assert "| ID | Name | Department |" in result
    assert "| Alice | R&D |" in result
    assert "[Tip: Pass `sheet_name` to read data from other available sheets.]" in result


@pytest.mark.asyncio
async def test_read_excel_specific_sheet(tmp_path: Path) -> None:
    """Verify explicit sheet selection."""
    excel_path = _create_sample_excel(tmp_path)
    result = await read_spreadsheet.invoke({
        "file_path": str(excel_path),
        "sheet_name": "Finances",
    })
    assert "Sheet: Finances" in result
    assert "**[Finances]** (active)" in result
    assert "| Quarter | Revenue | Cost |" in result
    assert "| 2025Q1 | $10,000 | $6,000 |" in result


@pytest.mark.asyncio
async def test_read_excel_pagination(tmp_path: Path) -> None:
    """Verify Excel pagination via start_row."""
    excel_path = _create_sample_excel(tmp_path)
    result = await read_spreadsheet.invoke({
        "file_path": str(excel_path),
        "sheet_name": "Employees",
        "start_row": 2,
        "max_rows": 1,
    })
    assert "Showing rows 2-2" in result
    assert "| Bob | Product |" in result
    assert "| Alice | R&D |" not in result


@pytest.mark.asyncio
async def test_read_excel_sheet_not_found(tmp_path: Path) -> None:
    """Verify error feedback when requested sheet does not exist."""
    excel_path = _create_sample_excel(tmp_path)
    result = await read_spreadsheet.invoke({
        "file_path": str(excel_path),
        "sheet_name": "NonExistentSheet",
    })
    assert "[ERROR]" in result
    assert "Sheet 'NonExistentSheet' not found" in result
    assert "Available sheets: ['Employees', 'Finances']" in result


@pytest.mark.asyncio
async def test_read_spreadsheet_file_size_limit(tmp_path: Path) -> None:
    """Verify rejection when spreadsheet exceeds 50MB size limit."""
    big_file = tmp_path / "huge.csv"
    big_file.write_text("ID,Val\n", encoding="utf-8")

    with patch(
        "jiuwenswarm.agents.harness.common.tools.spreadsheet_tools._get_file_size",
        return_value=60 * 1024 * 1024,
    ):
        result = await read_spreadsheet.invoke({"file_path": str(big_file)})
        assert "[ERROR]" in result
        assert "exceeds the 50MB limit" in result


@pytest.mark.asyncio
async def test_read_spreadsheet_unsupported_extension(tmp_path: Path) -> None:
    """Verify rejection of unsupported extensions."""
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("abc", encoding="utf-8")

    result = await read_spreadsheet.invoke({"file_path": str(txt_file)})
    assert "[ERROR]" in result
    assert "only accepts" in result


@pytest.mark.asyncio
async def test_read_spreadsheet_file_not_found(tmp_path: Path) -> None:
    """Verify missing file handling."""
    missing = tmp_path / "non_existent.xlsx"
    result = await read_spreadsheet.invoke({"file_path": str(missing)})
    assert "[ERROR]" in result
    assert "does not exist" in result


@pytest.mark.asyncio
async def test_read_csv_start_row_beyond_last_row(tmp_path: Path) -> None:
    """Verify out-of-range start_row reports the real range instead of a bogus one."""
    csv_file = _create_sample_csv(tmp_path)

    result = await read_spreadsheet.invoke({
        "file_path": str(csv_file),
        "start_row": 100,
        "max_rows": 10,
    })

    assert "beyond the last data row" in result
    assert "The last page starts at start_row=2" in result
    assert "Showing rows 100-100" not in result


@pytest.mark.asyncio
async def test_read_csv_header_only_reports_no_data_rows(tmp_path: Path) -> None:
    """Verify a header-only CSV is not reported as a one-row page."""
    csv_file = tmp_path / "header_only.csv"
    csv_file.write_text("ID,Value\n", encoding="utf-8")

    result = await read_spreadsheet.invoke({"file_path": str(csv_file)})

    assert "header only" in result
    assert "no data rows" in result
    assert "Showing rows" not in result
    assert "| ID | Value |" in result


@pytest.mark.asyncio
async def test_read_excel_header_only_reports_no_data_rows(tmp_path: Path) -> None:
    """Verify a header-only Excel sheet does not report a contradictory start_row range."""
    excel_path = tmp_path / "header_only.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Template"
    sheet.append(["ID", "Value"])
    workbook.save(excel_path)
    workbook.close()

    result = await read_spreadsheet.invoke({"file_path": str(excel_path)})

    assert "header only" in result
    assert "no data rows" in result
    assert "beyond the last data row" not in result
    assert "Showing rows" not in result
    assert "| ID | Value |" in result


@pytest.mark.asyncio
async def test_read_excel_start_row_beyond_last_row(tmp_path: Path) -> None:
    """Verify out-of-range start_row in Excel reports the total row count."""
    excel_path = _create_sample_excel(tmp_path)

    result = await read_spreadsheet.invoke({
        "file_path": str(excel_path),
        "sheet_name": "Employees",
        "start_row": 50,
    })

    assert "beyond the last data row" in result
    assert "2 data rows" in result
    assert "The last page starts at start_row=2" in result
    assert "Showing rows 50-50" not in result


@pytest.mark.asyncio
async def test_read_excel_last_page_reports_total_rows(tmp_path: Path) -> None:
    """Verify the row summary includes the total when the whole sheet was scanned."""
    excel_path = _create_sample_excel(tmp_path)

    result = await read_spreadsheet.invoke({
        "file_path": str(excel_path),
        "sheet_name": "Employees",
        "start_row": 2,
        "max_rows": 10,
    })

    assert "Showing rows 2-2 of 2" in result
