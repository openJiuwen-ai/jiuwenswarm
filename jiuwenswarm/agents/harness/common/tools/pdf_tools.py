# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Page-level PDF reading tool.

Extracts the text layer of a PDF with pdfplumber (bundled with openjiuwen).
Supports page ranges so large documents can be read incrementally, as the
wiki ingestion prompts instruct. Pages without a text layer (scanned PDFs)
are flagged so the model can fall back to vision tools.
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
_MAX_PAGES_PER_CALL = 100


@dataclass(frozen=True)
class ReadPdfRequest:
    pdf_path: str
    pages: tuple[int, ...] | None  # 1-based page numbers; None = all pages
    max_chars: int = DEFAULT_MAX_CHARS


def _parse_page_ranges(value: Any) -> tuple[int, ...] | None:
    """Parse ``pages`` input into sorted unique 1-based page numbers.

    Accepts an int (single page), a list of ints, or a string such as
    ``"1-5"``, ``"1,3,8-10"``. Empty / None means all pages.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("pages must be a page number, list, or range string like '1-5,8'")
    if isinstance(value, int):
        if value < 1:
            raise ValueError(f"page numbers are 1-based, got: {value}")
        return (value,)
    if isinstance(value, (list, tuple)):
        pages: set[int] = set()
        for entry in value:
            parsed = _parse_page_ranges(entry)
            if parsed:
                pages.update(parsed)
        return tuple(sorted(pages)) or None

    text = str(value).strip()
    if not text:
        return None
    pages = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"Invalid page range: {part!r}. Use forms like '1-5' or '3'.") from exc
            if start < 1 or end < start:
                raise ValueError(f"Invalid page range: {part!r} (pages are 1-based, start <= end)")
            pages.update(range(start, end + 1))
        else:
            try:
                page = int(part)
            except ValueError as exc:
                raise ValueError(f"Invalid page number: {part!r}") from exc
            if page < 1:
                raise ValueError(f"page numbers are 1-based, got: {page}")
            pages.add(page)
    return tuple(sorted(pages)) or None


def _resolve_pdf_path(value: str) -> Path:
    """Resolve ``pdf_path`` against Core's current primary workspace."""
    text = (value or "").strip()
    if not text:
        raise ValueError("pdf_path cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        try:
            from openjiuwen.core.sys_operation.cwd import get_workspace

            workspace = get_workspace()
        except Exception:
            workspace = None
        if not workspace:
            raise ValueError("read_pdf runtime workspace is unavailable")
        path = Path(workspace) / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"PDF file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"pdf_path is not a file: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"read_pdf only accepts .pdf files, got: {path.name}")
    return path


def _normalize_request(inputs: dict[str, Any]) -> ReadPdfRequest:
    pdf_path = str(inputs.get("pdf_path", "") or "").strip()
    if not pdf_path:
        raise ValueError("pdf_path cannot be empty.")
    pages = _parse_page_ranges(inputs.get("pages"))
    try:
        max_chars = int(inputs.get("max_chars", DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    max_chars = max(1_000, min(max_chars, _MAX_CHARS_CEILING))
    return ReadPdfRequest(pdf_path=pdf_path, pages=pages, max_chars=max_chars)


def _read_pdf_sync(req: ReadPdfRequest) -> str:
    # Validate the path before importing pdfplumber so path errors surface
    # clearly even in environments where the dependency is missing.
    path = _resolve_pdf_path(req.pdf_path)

    import pdfplumber

    blocks: list[str] = []
    empty_pages: list[int] = []
    with pdfplumber.open(str(path)) as pdf:
        total_pages = len(pdf.pages)
        if req.pages is not None:
            selected = [p for p in req.pages if p <= total_pages]
            out_of_range = [p for p in req.pages if p > total_pages]
        else:
            selected = list(range(1, total_pages + 1))
            out_of_range = []

        selected = selected[:_MAX_PAGES_PER_CALL]

        header = [f"PDF: {path.name} | total pages: {total_pages} | reading pages: "
                  + (_format_page_list(selected) if selected else "none")]
        # The header line above states the subset, but a passive fact is easy to
        # summarise past: say plainly what is missing and how to get it. Pages go
        # unread whenever the selection misses them, whether because `pages` named
        # a subset or because the per-call page cap truncated it, so the disclosure
        # keys off the pages actually missing rather than off `pages` being passed.
        chosen = set(selected)
        skipped = [p for p in range(1, total_pages + 1) if p not in chosen]
        if skipped:
            if total_pages <= _MAX_PAGES_PER_CALL:
                remedy = "Omit `pages` to read the whole document in one call."
            else:
                # Above the cap no single call can cover the document, so pointing
                # at the default read would send the model back here short again.
                remedy = (
                    f"At most {_MAX_PAGES_PER_CALL} pages are read per call, so no single "
                    "call covers this document — call read_pdf again with `pages` starting "
                    f"at {skipped[0]}."
                )
            header.append(
                f"[Partial read: {len(skipped)} of {total_pages} pages were NOT read "
                f"(pages {_format_page_list(skipped)}). {remedy} Any answer based on "
                "this call must state which pages it covers.]"
            )
        if out_of_range:
            header.append(
                f"[Note: requested page(s) {_format_page_list(out_of_range)} exceed "
                f"total page count {total_pages} and were skipped]"
            )
        blocks.append("\n".join(header))

        chars_used = 0
        for idx, page_num in enumerate(selected):
            page = pdf.pages[page_num - 1]
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                empty_pages.append(page_num)
                blocks.append(f"--- Page {page_num} ---\n[no text layer on this page]")
                continue
            remaining = req.max_chars - chars_used
            if remaining <= 0:
                unread = selected[idx:]
            else:
                truncated_here = len(page_text) > remaining
                if truncated_here:
                    page_text = page_text[:remaining] + "\n[... page truncated at max_chars ...]"
                chars_used += len(page_text)
                blocks.append(f"--- Page {page_num} ---\n{page_text}")
                if not truncated_here:
                    continue
                unread = selected[idx + 1:]
            note = (
                f"[Truncated at max_chars={req.max_chars}: the rest of the document "
                "was NOT read"
            )
            if unread:
                note += (
                    f"; unread pages: {_format_page_list(unread)} — "
                    "call read_pdf again with `pages` starting there"
                )
            note += (
                ". Raise `max_chars` to read more in one call. Any answer based on "
                "this call must state that the document was read only in part.]"
            )
            blocks.append(note)
            break

    if empty_pages:
        blocks.append(
            f"[Pages without extractable text: {_format_page_list(empty_pages)}. "
            "These are likely scanned images; use image/vision tools on rendered "
            "pages if their content is needed.]"
        )
    return "\n\n".join(blocks)


def _format_page_list(pages: list[int] | tuple[int, ...]) -> str:
    """Compress sorted page numbers into a compact range string, e.g. 1-3,7."""
    if not pages:
        return ""
    ordered = sorted(set(int(p) for p in pages))
    parts: list[str] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = page
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


@tool(
    name="read_pdf",
    description=(
        "Read the text content of a PDF file, optionally limited to specific pages. "
        "Use this tool when a PDF file path is available and its content is needed. "
        # Interpolated, not spelled out: the description is the only place the
        # model learns these bounds, so a changed constant must not leave it
        # stating the old one.
        f"Omitting `pages` reads the whole document, up to max_chars and {_MAX_PAGES_PER_CALL} pages, "
        "which is the right default — most documents fit in one call. Only when the return "
        "reports that pages were left unread, continue with `pages` from the first "
        "unread page until the document is covered. Any answer based on part of a "
        "document must state which pages it used. "
        "Input: pdf_path (local file path), optional pages (e.g. 3, '1-5' or '1,3,8-10'), "
        f"optional max_chars (default {DEFAULT_MAX_CHARS}). Pages without a text layer are reported "
        "as likely scanned images."
    ),
)
async def read_pdf(
    pdf_path: str,
    pages: Any = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    **kwargs: Any,
) -> str:
    _ = kwargs
    try:
        req = _normalize_request(
            {
                "pdf_path": pdf_path,
                "pages": pages,
                "max_chars": max_chars,
            }
        )
        logger.info("[read_pdf] path=%s pages=%s max_chars=%s", req.pdf_path, req.pages, req.max_chars)
        return await asyncio.to_thread(_read_pdf_sync, req)
    except Exception as exc:
        return f"[ERROR]: read_pdf failed: {exc}"
