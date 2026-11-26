# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured Word document (.docx) reading tool.

Extracts headings, paragraphs, and tables from Word (.docx) files using python-docx.
Preserves the document sequence (interleaved paragraphs and tables) and renders tables
as Markdown for clear model interpretation. Enforces a maximum character ceiling
to guard model context.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 50_000
_MAX_CHARS_CEILING = 200_000


@dataclass(frozen=True)
class ReadDocxRequest:
    """Normalized request parameters for reading a Word document."""

    docx_path: str
    start_element: int = 1
    max_chars: int = DEFAULT_MAX_CHARS


def _get_workspace() -> str | None:
    """Safely obtain runtime workspace path if available."""
    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_workspace

        return get_workspace()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _resolve_docx_path(value: str) -> Path:
    """Resolve ``docx_path`` against runtime workspace or filesystem."""
    text = (value or "").strip()
    if not text:
        raise ValueError("docx_path cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        workspace = _get_workspace()
        if not workspace:
            raise ValueError("read_docx runtime workspace is unavailable")
        path = Path(workspace) / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Word document file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"docx_path is not a file: {path}")
    if path.suffix.lower() != ".docx":
        raise ValueError(f"read_docx only accepts .docx files, got: {path.name}")
    return path


def _clean_docx_cell(text: str) -> str:
    """Normalize cell text for a single-line Markdown table cell."""
    return text.strip().replace("\r\n", " ").replace("\n", " ").replace("|", "\\|")


def _format_table_as_markdown(table: Any) -> str:
    """Format a python-docx Table object as a Markdown table."""
    rows = table.rows
    if not rows:
        return ""

    table_data: list[list[str]] = []
    max_cols = 0
    for row in rows:
        cell_texts = [_clean_docx_cell(cell.text) for cell in row.cells]
        max_cols = max(max_cols, len(cell_texts))
        table_data.append(cell_texts)

    if max_cols == 0 or not table_data:
        return ""

    header = list(table_data[0])
    while len(header) < max_cols:
        header.append("")
    md_lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * max_cols) + " |",
    ]

    for row_cells in table_data[1:]:
        padded = list(row_cells)
        while len(padded) < max_cols:
            padded.append("")
        md_lines.append("| " + " | ".join(padded) + " |")

    return "\n".join(md_lines)


_HEADING_PREFIXES: tuple[tuple[str, str], ...] = (
    ("heading 1", "# "),
    ("heading 2", "## "),
    ("heading 3", "### "),
    ("heading 4", "#### "),
)


def _format_paragraph(paragraph: Any) -> str:
    """Format a python-docx Paragraph according to its style."""
    text = paragraph.text.strip()
    if not text:
        return ""
    style_name = (paragraph.style.name if paragraph.style else "").lower()
    for token, prefix in _HEADING_PREFIXES:
        if token in style_name:
            return f"{prefix}{text}"
    if "list" in style_name or "bullet" in style_name:
        return f"- {text}"
    return text


def _extract_element_markdown(element: Any, doc: Any) -> str:
    """Extract Markdown representation for a single body element."""
    from docx.table import Table  # pylint: disable=import-outside-toplevel
    from docx.text.paragraph import Paragraph  # pylint: disable=import-outside-toplevel

    if element.tag.endswith("}p"):
        return _format_paragraph(Paragraph(element, doc))
    if element.tag.endswith("}tbl"):
        return _format_table_as_markdown(Table(element, doc))
    return ""


def _collect_docx_blocks(
    doc: Any,
    req: ReadDocxRequest,
) -> tuple[list[str], int, bool, bool]:
    """Iterate body elements from ``start_element`` within ``max_chars`` budget.

    The last flag is True when the first pending element alone exceeds the budget.
    """
    blocks: list[str] = []
    current_chars = 0
    truncated = False
    stalled = False
    last_processed_idx = req.start_element - 1

    for idx, element in enumerate(doc.element.body, 1):
        if idx < req.start_element:
            continue
        content = _extract_element_markdown(element, doc)
        if not content:
            last_processed_idx = idx
            continue

        candidate_len = current_chars + len(content) + 2
        if candidate_len > req.max_chars:
            allowed_remaining = req.max_chars - current_chars
            if allowed_remaining > 50:
                blocks.append(content[:allowed_remaining] + " ...")
            truncated = True
            stalled = not blocks or current_chars == 0
            break

        blocks.append(content)
        current_chars += len(content) + 2
        last_processed_idx = idx

    return blocks, last_processed_idx, truncated, stalled


def _empty_read_notice(
    req: ReadDocxRequest,
    paragraph_count: int,
    table_count: int,
    total_elements: int,
) -> str:
    """Explain a read that produced no readable content."""
    empty_notice = "[Document is empty or contains no readable text/tables.]"
    if total_elements == 0 or req.start_element == 1:
        return empty_notice
    if req.start_element > total_elements:
        return (
            f"[start_element={req.start_element} is past the end of the document, which has "
            f"{total_elements} body elements ({paragraph_count} paragraphs, {table_count} tables). "
            f"Call read_docx with `start_element<={total_elements}` to read it again.]"
        )
    if paragraph_count == 0 and table_count == 0:
        return empty_notice
    return (
        f"[No readable text or tables at or after element {req.start_element}. "
        f"The document is not empty ({paragraph_count} paragraphs, {table_count} tables, "
        f"{total_elements} body elements), but nothing readable remains from this position. "
        f"Call read_docx with a smaller `start_element` (1..{total_elements}) to read earlier content.]"
    )


def _read_docx_sync(req: ReadDocxRequest) -> str:
    """Synchronously parse and format the .docx document contents."""
    path = _resolve_docx_path(req.docx_path)

    import docx  # pylint: disable=import-outside-toplevel

    try:
        doc = docx.Document(str(path))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ValueError(f"Failed to open .docx document: {exc}") from exc

    paragraph_count = len(doc.paragraphs)
    table_count = len(doc.tables)
    total_elements = len(doc.element.body)
    blocks, last_processed_idx, truncated, stalled = _collect_docx_blocks(doc, req)

    output_body = "\n\n".join(blocks)
    if truncated and stalled:
        output_body += (
            f"\n\n[Document truncated at {req.max_chars} characters. "
            f"Element {last_processed_idx + 1} of {total_elements} alone exceeds "
            f"max_chars, so start_element cannot advance past it. "
            f"Call read_docx again with a larger `max_chars` "
            f"(keep `start_element={last_processed_idx + 1}`) to read it in full.]"
        )
    elif truncated:
        output_body += (
            f"\n\n[Document truncated at {req.max_chars} characters. "
            f"Read up to element {last_processed_idx} of {total_elements} "
            f"({paragraph_count} paragraphs, {table_count} tables). "
            f"Call read_docx with `start_element={last_processed_idx + 1}` to continue reading.]"
        )
    elif not output_body:
        output_body = _empty_read_notice(req, paragraph_count, table_count, total_elements)

    return output_body


@tool(
    name="read_docx",
    description=(
        "Read structured text content from a Word document (.docx), including headings, "
        "paragraphs, and tables formatted as Markdown. "
        "Use this tool when a local Word document (.docx) path is available and its content "
        "or tabular data is needed. "
        "Input: docx_path (local file path), optional start_element (default 1), "
        "optional max_chars (default 50000, up to 200000)."
    ),
)
async def read_docx(
    docx_path: str,
    start_element: int = 1,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Read and format structured text and tables from a Word document."""
    try:
        try:
            safe_start = max(1, int(start_element))
        except (TypeError, ValueError):
            safe_start = 1

        try:
            safe_max = int(max_chars)
        except (TypeError, ValueError):
            safe_max = DEFAULT_MAX_CHARS
        safe_max = max(1_000, min(safe_max, _MAX_CHARS_CEILING))

        req = ReadDocxRequest(
            docx_path=str(docx_path or "").strip(),
            start_element=safe_start,
            max_chars=safe_max,
        )
        logger.info(
            "[read_docx] path=%s start=%s max_chars=%s",
            req.docx_path,
            req.start_element,
            req.max_chars,
        )
        return await asyncio.to_thread(_read_docx_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: read_docx failed: {exc}"
