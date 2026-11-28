# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured spreadsheet reading tool for Excel (.xlsx, .xlsm) and CSV (.csv) files.

Extracts tabular data formatted as Markdown tables. Uses openpyxl with read_only=True
for efficient, low-memory Excel streaming, and Python's standard csv module with encoding
fallback (UTF-8, GBK) for CSV files. Supports sheet navigation, pagination via start_row,
and size/row caps to protect the model's context window.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 50
_MAX_ROWS_CEILING = 500
DEFAULT_MAX_CHARS = 30_000
_MAX_CHARS_CEILING = 100_000
_MAX_CELL_CHARS = 120
_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB file size safeguard

SUPPORTED_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".csv"}


@dataclass(frozen=True)
class ReadSpreadsheetRequest:
    """Normalized request parameters for reading a spreadsheet."""

    file_path: str
    sheet_name: str | None = None
    start_row: int = 1
    max_rows: int = DEFAULT_MAX_ROWS
    max_chars: int = DEFAULT_MAX_CHARS


def _get_file_size(path: Path) -> int:
    """Return file size in bytes."""
    return path.stat().st_size


def _get_workspace() -> str | None:
    """Safely obtain runtime workspace path if available."""
    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_workspace

        return get_workspace()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _resolve_spreadsheet_path(value: str) -> Path:
    """Resolve ``file_path`` against runtime workspace or filesystem."""
    text = (value or "").strip()
    if not text:
        raise ValueError("file_path cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        workspace = _get_workspace()
        if not workspace:
            raise ValueError("read_spreadsheet runtime workspace is unavailable")
        path = Path(workspace) / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Spreadsheet file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"file_path is not a file: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported_str = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"read_spreadsheet only accepts [{supported_str}], got: {path.name}")

    file_size = _get_file_size(path)
    if file_size > _MAX_FILE_SIZE_BYTES:
        size_mb = file_size / (1024 * 1024)
        raise ValueError(
            f"Spreadsheet file '{path.name}' ({size_mb:.1f}MB) exceeds the 50MB limit."
        )
    return path


def _format_cell_value(val: Any) -> str:
    """Clean and truncate cell value to fit neatly in a Markdown table cell."""
    if val is None:
        return ""
    text = str(val).strip().replace("\r\n", " ").replace("\n", " ").replace("|", "\\|")
    if len(text) > _MAX_CELL_CHARS:
        return text[:_MAX_CELL_CHARS] + "..."
    return text


def _is_non_empty_row(row: Any) -> bool:
    """Return True if at least one cell in the row contains non-whitespace text."""
    for cell in row:
        if cell is not None and str(cell).strip():
            return True
    return False


def _build_markdown_table(rows: list[list[str]]) -> str:
    """Convert a 2D grid of strings into a standard Markdown table."""
    if not rows:
        return "[Empty table]"

    max_cols = max(len(r) for r in rows)
    if max_cols == 0:
        return "[Empty table]"

    header = list(rows[0])
    while len(header) < max_cols:
        header.append("")
    if not any(header):
        header = [f"Col {i + 1}" for i in range(max_cols)]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * max_cols) + " |",
    ]

    for r in rows[1:]:
        row_cells = list(r)
        while len(row_cells) < max_cols:
            row_cells.append("")
        lines.append("| " + " | ".join(row_cells) + " |")

    return "\n".join(lines)


def _decode_csv_bytes(path: Path) -> str:
    """Read and decode CSV bytes using standard encodings."""
    raw_data = path.read_bytes()
    if not raw_data:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw_data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Unable to decode CSV file '{path.name}' with standard encodings.")


def _stream_csv_page(
    decoded_text: str,
    start_row: int,
    max_rows: int,
) -> tuple[list[str] | None, list[list[str]], int]:
    """Stream CSV rows and format only the header and requested data page."""
    reader = csv.reader(io.StringIO(decoded_text))
    header: list[str] | None = None
    paged_rows: list[list[str]] = []
    data_count = 0
    start_idx = max(0, start_row - 1)
    end_idx = start_idx + max_rows

    for raw_row in reader:
        if not _is_non_empty_row(raw_row):
            continue
        if header is None:
            header = [_format_cell_value(c) for c in raw_row]
            continue
        if start_idx <= data_count < end_idx:
            paged_rows.append([_format_cell_value(c) for c in raw_row])
        data_count += 1

    return header, paged_rows, data_count


def _read_csv(path: Path, req: ReadSpreadsheetRequest) -> str:
    """Read CSV file with automatic encoding fallback and pagination."""
    if path.stat().st_size == 0:
        return "[CSV file is empty]"

    decoded_text = _decode_csv_bytes(path)
    if not decoded_text.strip():
        return "[CSV file is empty]"

    header, paged_rows, total_data_count = _stream_csv_page(
        decoded_text,
        req.start_row,
        req.max_rows,
    )
    if header is None:
        return "[CSV file is empty]"

    start_idx = max(0, req.start_row - 1)
    if not paged_rows and start_idx >= total_data_count:
        if total_data_count == 0:
            return (
                f"**File: {path.name} (CSV, header only)**\n\n"
                f"{_build_markdown_table([header])}\n\n"
                "[Note: The file has a header row but no data rows.]"
            )
        return (
            f"**File: {path.name} (CSV, {total_data_count} data rows)**\n\n"
            f"[Note: start_row={req.start_row} is beyond the last data row. "
            f"The last page starts at start_row={total_data_count}.]"
        )
    has_more_rows = (start_idx + len(paged_rows)) < total_data_count
    next_start_row = start_idx + len(paged_rows) + 1

    table_md = _build_markdown_table([header] + paged_rows)
    end_row = start_idx + len(paged_rows) if paged_rows else start_idx + 1
    showing_info = f"rows {start_idx + 1}-{end_row} of {total_data_count}"
    output = f"**File: {path.name} (CSV, Showing {showing_info})**\n\n{table_md}"

    if has_more_rows:
        output += (
            f"\n\n[Note: Additional rows available. Call `read_spreadsheet` with "
            f"`start_row={next_start_row}` to read the next page.]"
        )

    if len(output) > req.max_chars:
        output = output[:req.max_chars] + f"\n\n[Output truncated at {req.max_chars} chars.]"

    return output


def _stream_excel_page(
    ws: Any,
    start_row: int,
    max_rows: int,
) -> tuple[list[str] | None, list[list[str]], bool, int | None]:
    """Stream non-empty rows from an openpyxl worksheet for the requested page.

    Returns the header, the requested page of rows, whether more rows follow the
    page, and the total data row count when the whole sheet was scanned (``None``
    when the scan stopped early, so the total is unknown).
    """
    header: list[str] | None = None
    data_rows: list[list[str]] = []
    has_more_rows = False
    data_row_count = 0
    total_rows: int | None = None
    target_start = max(1, start_row)
    target_end = target_start + max_rows

    for row_tuple in ws.iter_rows(values_only=True):
        if not _is_non_empty_row(row_tuple):
            continue
        if header is None:
            header = [_format_cell_value(c) for c in row_tuple]
            continue
        data_row_count += 1
        if target_start <= data_row_count < target_end:
            data_rows.append([_format_cell_value(c) for c in row_tuple])
        elif data_row_count >= target_end:
            has_more_rows = True
            break
    else:
        total_rows = data_row_count

    return header, data_rows, has_more_rows, total_rows


def _format_sheets_nav(sheetnames: list[str], active_sheet_name: str) -> str:
    """Build the Markdown sheet navigation summary line."""
    nav_parts: list[str] = []
    for name in sheetnames:
        if name == active_sheet_name:
            nav_parts.append(f"**[{name}]** (active)")
        else:
            nav_parts.append(name)
    return "Sheets: " + ", ".join(nav_parts)


def _format_excel_output(
    file_name: str,
    sheetnames: list[str],
    active_sheet_name: str,
    page: tuple[list[str], list[list[str]], bool, int | None],
    req: ReadSpreadsheetRequest,
) -> str:
    """Format extracted Excel sheet rows and navigation notes into Markdown."""
    header, data_rows, has_more_rows, total_rows = page
    sheets_nav = _format_sheets_nav(sheetnames, active_sheet_name)
    target_start = max(1, req.start_row)
    if not data_rows and total_rows == 0:
        return (
            f"**File: {file_name} | Sheet: {active_sheet_name} (header only)**\n"
            f"{sheets_nav}\n\n"
            f"{_build_markdown_table([header])}\n\n"
            "[Note: The sheet has a header row but no data rows.]"
        )
    if not data_rows and total_rows is not None and target_start > total_rows:
        return (
            f"**File: {file_name} | Sheet: {active_sheet_name} ({total_rows} data rows)**\n"
            f"{sheets_nav}\n\n"
            f"[Note: start_row={req.start_row} is beyond the last data row. "
            f"The last page starts at start_row={max(1, total_rows)}.]"
        )
    table_md = _build_markdown_table([header] + data_rows)
    next_start_row = target_start + len(data_rows)
    end_row = target_start + len(data_rows) - 1 if data_rows else target_start
    total_suffix = f" of {total_rows}" if total_rows is not None else ""
    summary = (
        f"**File: {file_name} | Sheet: {active_sheet_name} "
        f"(Showing rows {target_start}-{end_row}{total_suffix})**\n{sheets_nav}"
    )
    output = f"{summary}\n\n{table_md}"

    if has_more_rows:
        output += (
            f"\n\n[Note: Additional rows available in sheet '{active_sheet_name}'. "
            f"Call `read_spreadsheet` with `start_row={next_start_row}` to inspect more.]"
        )
    if len(sheetnames) > 1 and not req.sheet_name:
        output += "\n[Tip: Pass `sheet_name` to read data from other available sheets.]"
    if len(output) > req.max_chars:
        output = output[:req.max_chars] + f"\n\n[Output truncated at {req.max_chars} chars.]"
    return output


def _read_excel(path: Path, req: ReadSpreadsheetRequest) -> str:
    """Read Excel workbook sheets and stream rows efficiently."""
    import openpyxl  # pylint: disable=import-outside-toplevel

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ValueError(f"Failed to open Excel file '{path.name}': {exc}") from exc

    try:
        sheetnames = list(wb.sheetnames)
        if not sheetnames:
            return "[Excel workbook contains no sheets]"

        active_sheet_name = req.sheet_name or sheetnames[0]
        if active_sheet_name not in sheetnames:
            sheets_display = ", ".join(repr(s) for s in sheetnames)
            raise ValueError(
                f"Sheet '{active_sheet_name}' not found. Available sheets: [{sheets_display}]."
            )

        page = _stream_excel_page(
            wb[active_sheet_name],
            req.start_row,
            req.max_rows,
        )
        if page[0] is None:
            sheets_nav = _format_sheets_nav(sheetnames, active_sheet_name)
            return f"{sheets_nav}\n\n[Sheet '{active_sheet_name}' is empty]"

        return _format_excel_output(
            path.name,
            sheetnames,
            active_sheet_name,
            page,
            req,
        )
    finally:
        wb.close()


def _clamp_int(value: Any, default: int, low: int, high: int | None = None) -> int:
    """Parse integer parameter with fallback default and bounds clamping."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if high is not None:
        return max(low, min(parsed, high))
    return max(low, parsed)


def _read_spreadsheet_sync(req: ReadSpreadsheetRequest) -> str:
    """Synchronously dispatch spreadsheet reading by file extension."""
    path = _resolve_spreadsheet_path(req.file_path)
    if path.suffix.lower() == ".csv":
        return _read_csv(path, req)
    return _read_excel(path, req)


@tool(
    name="read_spreadsheet",
    description=(
        "Read tabular data from Excel (.xlsx, .xlsm, .xltx) or CSV (.csv) files as "
        "Markdown tables. Supports sheet selection, pagination via start_row, and row limits. "
        "Input: file_path, optional sheet_name, optional start_row (default 1), "
        "optional max_rows (default 50, up to 500), optional max_chars (default 30000)."
    ),
)
async def read_spreadsheet(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    file_path: str,
    sheet_name: str | None = None,
    start_row: int = 1,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Read and format tabular data from an Excel workbook or CSV file."""
    try:
        req = ReadSpreadsheetRequest(
            file_path=str(file_path or "").strip(),
            sheet_name=str(sheet_name).strip() if sheet_name else None,
            start_row=_clamp_int(start_row, 1, 1),
            max_rows=_clamp_int(max_rows, DEFAULT_MAX_ROWS, 1, _MAX_ROWS_CEILING),
            max_chars=_clamp_int(max_chars, DEFAULT_MAX_CHARS, 1_000, _MAX_CHARS_CEILING),
        )
        logger.info(
            "[read_spreadsheet] path=%s sheet=%s start_row=%s max_rows=%s max_chars=%s",
            req.file_path,
            req.sheet_name,
            req.start_row,
            req.max_rows,
            req.max_chars,
        )
        return await asyncio.to_thread(_read_spreadsheet_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: read_spreadsheet failed: {exc}"
