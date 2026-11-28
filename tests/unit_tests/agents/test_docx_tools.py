# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the Word document reading tool (read_docx)."""

from __future__ import annotations

from pathlib import Path

import docx
import pytest

from jiuwenswarm.agents.harness.common.tools.docx_tools import (
    _format_paragraph,
    _format_table_as_markdown,
    read_docx,
)


def _create_sample_docx(tmp_path: Path) -> Path:
    """Create a sample docx file with headings, paragraphs, and a table."""
    doc = docx.Document()
    doc.add_heading("Document Title", level=1)
    doc.add_paragraph("This is the introductory paragraph.")
    doc.add_heading("Section 1: Data Summary", level=2)

    table = doc.add_table(rows=3, cols=3)
    data = [
        ["Name", "Role", "Score"],
        ["Alice", "Engineer", "95"],
        ["Bob", "Designer", "88"],
    ]
    for row_idx, row in enumerate(data):
        for col_idx, text in enumerate(row):
            table.cell(row_idx, col_idx).text = text

    doc.add_paragraph("Concluding notes after table.")
    file_path = tmp_path / "test_sample.docx"
    doc.save(str(file_path))
    return file_path


def _create_empty_docx(tmp_path: Path) -> Path:
    """Create an empty docx document."""
    doc = docx.Document()
    file_path = tmp_path / "empty.docx"
    doc.save(str(file_path))
    return file_path


def test_paragraph_and_table_formatting() -> None:
    """Verify internal formatting helpers for paragraphs and tables."""
    doc = docx.Document()
    p_head = doc.add_heading("My Heading", level=1)
    assert _format_paragraph(p_head) == "# My Heading"

    p_normal = doc.add_paragraph("Regular text")
    assert _format_paragraph(p_normal) == "Regular text"

    tbl = doc.add_table(rows=0, cols=0)
    assert _format_table_as_markdown(tbl) == ""


@pytest.mark.asyncio
async def test_read_docx_success(tmp_path: Path) -> None:
    """Verify standard extraction of headings, paragraphs, and markdown tables."""
    doc_path = _create_sample_docx(tmp_path)
    result = await read_docx.invoke({"docx_path": str(doc_path)})

    assert "# Document Title" in result
    assert "This is the introductory paragraph." in result
    assert "## Section 1: Data Summary" in result
    assert "| Name | Role | Score |" in result
    assert "| --- | --- | --- |" in result
    assert "| Alice | Engineer | 95 |" in result
    assert "| Bob | Designer | 88 |" in result
    assert "Concluding notes after table." in result


@pytest.mark.asyncio
async def test_read_docx_empty(tmp_path: Path) -> None:
    """Verify empty document behavior."""
    doc_path = _create_empty_docx(tmp_path)
    result = await read_docx.invoke({"docx_path": str(doc_path)})
    assert "[Document is empty" in result


@pytest.mark.asyncio
async def test_read_docx_truncation(tmp_path: Path) -> None:
    """Verify max_chars truncation behavior on a large document."""
    doc = docx.Document()
    for i in range(50):
        doc.add_paragraph(
            f"Paragraph {i}: " + "Structured office document paragraph text " * 5
        )
    large_file = tmp_path / "large.docx"
    doc.save(str(large_file))

    result = await read_docx.invoke({"docx_path": str(large_file), "max_chars": 1000})
    assert "[Document truncated at 1000 characters" in result
    assert "start_element=" in result


@pytest.mark.asyncio
async def test_read_docx_oversized_first_element_hints_max_chars(tmp_path: Path) -> None:
    """A first element larger than max_chars must advise raising max_chars."""
    doc = docx.Document()
    doc.add_paragraph("Intro")
    table = doc.add_table(rows=200, cols=4)
    for row in table.rows:
        for cell in row.cells:
            cell.text = "cell-content-value"
    big_file = tmp_path / "big_table.docx"
    doc.save(str(big_file))

    result = await read_docx.invoke(
        {"docx_path": str(big_file), "start_element": 2, "max_chars": 500}
    )
    assert "alone exceeds max_chars" in result
    assert "larger `max_chars`" in result

    full = await read_docx.invoke(
        {"docx_path": str(big_file), "start_element": 2, "max_chars": 50000}
    )
    assert "alone exceeds" not in full


@pytest.mark.asyncio
async def test_read_docx_pagination(tmp_path: Path) -> None:
    """Verify start_element pagination behavior on a multi-paragraph document."""
    doc = docx.Document()
    for i in range(10):
        doc.add_paragraph(f"UniqueParagraph_{i}")
    page_file = tmp_path / "page_test.docx"
    doc.save(str(page_file))

    result = await read_docx.invoke({
        "docx_path": str(page_file),
        "start_element": 6,
    })
    assert "UniqueParagraph_5" in result
    assert "UniqueParagraph_0" not in result


@pytest.mark.asyncio
async def test_read_docx_start_element_on_non_readable_tail(tmp_path: Path) -> None:
    """Starting at a non-readable body element must not claim the document is empty."""
    doc = docx.Document()
    for i in range(3):
        doc.add_paragraph(f"Paragraph_{i}")
    tail_file = tmp_path / "non_readable_tail.docx"
    doc.save(str(tail_file))

    total_elements = len(docx.Document(str(tail_file)).element.body)
    result = await read_docx.invoke({
        "docx_path": str(tail_file),
        "start_element": total_elements,
    })
    assert "No readable text or tables at or after element" in result
    assert "not empty" in result
    assert "[Document is empty" not in result


@pytest.mark.asyncio
async def test_read_docx_start_element_past_document_end(tmp_path: Path) -> None:
    """A start position beyond the last body element must report the real document size."""
    doc = docx.Document()
    doc.add_paragraph("OnlyParagraph")
    past_end_file = tmp_path / "past_end.docx"
    doc.save(str(past_end_file))

    total_elements = len(docx.Document(str(past_end_file)).element.body)
    result = await read_docx.invoke({
        "docx_path": str(past_end_file),
        "start_element": total_elements + 5,
    })
    assert "past the end of the document" in result
    assert f"{total_elements} body elements" in result
    assert "[Document is empty" not in result


@pytest.mark.asyncio
async def test_read_docx_empty_body_is_not_reported_as_out_of_range(tmp_path: Path) -> None:
    """A document with an empty body must report emptiness instead of an out-of-range start."""
    doc = docx.Document()
    doc.element.body.clear_content()
    empty_file = tmp_path / "empty_body.docx"
    doc.save(str(empty_file))

    result = await read_docx.invoke({"docx_path": str(empty_file)})

    assert "[Document is empty" in result
    assert "past the end of the document" not in result
    assert "start_element<=" not in result


@pytest.mark.asyncio
async def test_read_docx_blank_paragraphs_report_empty(tmp_path: Path) -> None:
    """Paragraphs without readable text must not be claimed as readable content."""
    doc = docx.Document()
    doc.add_paragraph("")
    doc.add_paragraph("")
    blank_file = tmp_path / "blank_paragraphs.docx"
    doc.save(str(blank_file))

    result = await read_docx.invoke({"docx_path": str(blank_file)})

    assert "[Document is empty" in result
    assert "not empty" not in result
    assert "smaller `start_element`" not in result


@pytest.mark.asyncio
async def test_read_docx_non_existent(tmp_path: Path) -> None:
    """Verify error reporting on missing file."""
    fake_path = tmp_path / "does_not_exist.docx"
    result = await read_docx.invoke({"docx_path": str(fake_path)})
    assert "[ERROR]" in result
    assert "does not exist" in result


@pytest.mark.asyncio
async def test_read_docx_invalid_extension(tmp_path: Path) -> None:
    """Verify rejection of non-docx files."""
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("hello", encoding="utf-8")
    result = await read_docx.invoke({"docx_path": str(txt_file)})
    assert "[ERROR]" in result
    assert "only accepts .docx files" in result


@pytest.mark.asyncio
async def test_read_docx_empty_path() -> None:
    """Verify rejection of empty path input."""
    result = await read_docx.invoke({"docx_path": ""})
    assert "[ERROR]" in result
    assert "cannot be empty" in result
