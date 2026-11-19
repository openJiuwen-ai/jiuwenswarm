# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Slide-level PowerPoint (.pptx) presentation reading tool.

Extracts text, slide titles, tables, and speaker notes from PPTX presentations
into clean Markdown. Features a dual-mode extractor: uses python-pptx when
installed, and seamlessly falls back to an OpenXML zip parser when running in
lightweight environments without third-party dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 50_000
_MAX_CHARS_CEILING = 200_000
_MAX_SLIDE_SPAN = 500
_MAX_TOTAL_SLIDES = 1000
_REL_ID_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _get_workspace() -> str | None:
    """Safely obtain runtime workspace path if available."""
    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_workspace

        return get_workspace()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


@dataclass(frozen=True)
class ReadPptxRequest:
    """Normalized request parameters for reading a PowerPoint presentation."""

    pptx_path: str
    slides: tuple[int, ...] | None
    max_chars: int = DEFAULT_MAX_CHARS


def _parse_single_slide_part(part: str) -> set[int]:
    """Parse one comma-separated token ('3' or '1-5') into slide numbers."""
    if "-" not in part:
        try:
            num = int(part)
        except ValueError as exc:
            raise ValueError(f"Invalid slide number: {part!r}") from exc
        if num < 1:
            raise ValueError(f"Slide numbers must be >= 1, got: {num}")
        return {num}

    start_text, _, end_text = part.partition("-")
    try:
        start, end = int(start_text.strip()), int(end_text.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid slide range: {part!r}") from exc
    if start < 1 or end < 1:
        raise ValueError(f"Slide numbers must be >= 1, got range: {part!r}")
    if start > end:
        raise ValueError(f"Start of range exceeds end: {part!r}")
    if end - start > _MAX_SLIDE_SPAN:
        raise ValueError(
            f"Slide range span too large ({end - start} > {_MAX_SLIDE_SPAN}): {part!r}"
        )
    return set(range(start, end + 1))


def _parse_slide_ranges(value: Any) -> tuple[int, ...] | None:
    """Parse slides input into sorted unique 1-based slide numbers."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(
            "slides must be an integer, list of integers, or range string like '1-3,5'"
        )
    if isinstance(value, int):
        if value < 1:
            raise ValueError(f"slide numbers are 1-based, got: {value}")
        return (value,)
    if isinstance(value, (list, tuple)):
        slides: set[int] = set()
        for entry in value:
            parsed = _parse_slide_ranges(entry)
            if parsed:
                slides.update(parsed)
        if len(slides) > _MAX_TOTAL_SLIDES:
            raise ValueError(f"Too many slide numbers requested (> {_MAX_TOTAL_SLIDES})")
        return tuple(sorted(slides)) or None

    text = str(value).strip()
    if not text:
        return None
    slides_set: set[int] = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        if part:
            slides_set.update(_parse_single_slide_part(part))

    if len(slides_set) > _MAX_TOTAL_SLIDES:
        raise ValueError(f"Too many slide numbers requested (> {_MAX_TOTAL_SLIDES})")
    return tuple(sorted(slides_set)) or None


def _clean_table_cell(cell: str) -> str:
    """Normalize a single table cell string for Markdown table formatting."""
    return cell.replace("\n", " ").replace("|", "\\|").strip()


def _format_markdown_table(rows: list[list[str]]) -> str:
    """Convert a 2D list of cell strings into a clean Markdown table."""
    if not rows or not any(rows):
        return ""
    max_cols = max(len(row) for row in rows)
    if max_cols == 0:
        return ""
    norm_rows: list[list[str]] = []
    for row in rows:
        norm = [_clean_table_cell(cell) for cell in row]
        if len(norm) < max_cols:
            norm.extend([""] * (max_cols - len(norm)))
        norm_rows.append(norm)

    header = norm_rows[0]
    header_line = "| " + " | ".join(header) + " |"
    sep_line = "| " + " | ".join(["---"] * max_cols) + " |"
    data_lines = ["| " + " | ".join(r) + " |" for r in norm_rows[1:]]
    return "\n".join([header_line, sep_line] + data_lines)


def _resolve_pptx_path(raw_path: str) -> Path:
    """Resolve presentation file path against workspace."""
    cleaned = raw_path.strip().strip("'\"")
    path = Path(cleaned).expanduser()
    ws = _get_workspace()
    if not path.is_absolute():
        if ws is not None:
            ws_path = Path(ws).resolve()
            resolved = (ws_path / path).resolve()
            if not resolved.is_relative_to(ws_path):
                raise ValueError(f"Path must be inside workspace: {raw_path}")
        else:
            raise ValueError("read_pptx runtime workspace is unavailable")
    else:
        resolved = path.resolve()

    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {raw_path}")
    if not resolved.is_file():
        raise ValueError(f"Target is not a file: {raw_path}")
    if resolved.suffix.lower() != ".pptx":
        raise ValueError(
            f"Unsupported file format '{resolved.suffix}'. Only .pptx is supported."
        )
    return resolved


def _normalize_slide_rel_target(target: str) -> str:
    """Normalize a relationship target path relative to the archive root."""
    if target.startswith("/"):
        return target.lstrip("/")
    if not target.startswith("ppt/"):
        return f"ppt/{target}"
    return target


def _parse_slide_rel_map(rels_xml: bytes) -> dict[str, str]:
    """Map relationship IDs to slide XML archive paths."""
    rels_root = ET.fromstring(rels_xml)
    rel_map: dict[str, str] = {}
    for rel in rels_root:
        r_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target", "")
        if r_id and "slide" in target.lower():
            rel_map[r_id] = _normalize_slide_rel_target(target)
    return rel_map


def _get_ordered_slide_paths(zf: zipfile.ZipFile) -> list[str]:
    """Determine slide presentation order from presentation.xml or fallback to filenames."""
    names_set = set(zf.namelist())
    try:
        rel_map = _parse_slide_rel_map(zf.read("ppt/_rels/presentation.xml.rels"))
        pres_root = ET.fromstring(zf.read("ppt/presentation.xml"))
        ordered_targets: list[str] = []
        for elem in pres_root.iter():
            if elem.tag.endswith("sldId"):
                r_id = elem.attrib.get(_REL_ID_ATTR)
                if r_id and r_id in rel_map:
                    ordered_targets.append(rel_map[r_id])

        if ordered_targets and all(t in names_set for t in ordered_targets):
            return ordered_targets
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("Fallback to filename slide ordering: %s", exc)

    slide_files: list[tuple[int, str]] = []
    for name in zf.namelist():
        match = re.match(r"^ppt/slides/slide(\d+)\.xml$", name)
        if match:
            slide_files.append((int(match.group(1)), name))
    slide_files.sort(key=lambda item: item[0])
    return [name for _, name in slide_files]


def _collect_element_texts(node: ET.Element) -> list[str]:
    """Collect all non-empty text runs (`<a:t>`) under an XML element."""
    texts: list[str] = []
    for child in node.iter():
        if child.tag.endswith("t") and child.text:
            texts.append(child.text)
    return texts


def _resolve_notes_path(zf: zipfile.ZipFile, slide_path: str) -> str:
    """Resolve the notesSlide archive path via the slide's relationships."""
    slide_name = slide_path.rsplit("/", 1)[-1]
    rels_path = f"ppt/slides/_rels/{slide_name}.rels"
    try:
        for rel in ET.fromstring(zf.read(rels_path)):
            if not rel.attrib.get("Type", "").endswith("notesSlide"):
                continue
            target = rel.attrib.get("Target", "")
            if target.startswith("/"):
                return target.lstrip("/")
            return posixpath.normpath(posixpath.join("ppt/slides", target))
    except (KeyError, ET.ParseError) as exc:
        logger.debug("No notes relationship for %s: %s", slide_path, exc)
    match = re.search(r"slide(\d+)\.xml$", slide_name)
    return f"ppt/notesSlides/notesSlide{match.group(1)}.xml" if match else ""


def _extract_slide_notes(zf: zipfile.ZipFile, slide_path: str) -> str:
    """Attempt to extract speaker notes for a slide from OpenXML."""
    npath = _resolve_notes_path(zf, slide_path)
    if not npath or npath not in zf.namelist():
        return ""
    try:
        root = ET.fromstring(zf.read(npath))
        notes: list[str] = []
        for body in root.iter():
            if not body.tag.endswith("}txBody"):
                continue
            for para in body:
                if not para.tag.endswith("}p"):
                    continue
                line = "".join(_collect_element_texts(para)).strip()
                if line and not line.isdigit():
                    notes.append(line)
        return "\n".join(notes)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("Failed to parse notes slide %s: %s", npath, exc)
        return ""


def _extract_openxml_table(tbl_elem: ET.Element) -> str:
    """Extract a Markdown table from an OpenXML `<a:tbl>` element."""
    table_rows: list[list[str]] = []
    for tr in tbl_elem:
        if not tr.tag.endswith("tr"):
            continue
        row_cells: list[str] = []
        for tc in tr:
            if tc.tag.endswith("tc"):
                row_cells.append(" ".join(_collect_element_texts(tc)).strip())
        if row_cells:
            table_rows.append(row_cells)
    return _format_markdown_table(table_rows) if table_rows else ""


def _extract_openxml_shape_text(sp_elem: ET.Element) -> str:
    """Extract paragraph text from an OpenXML `<p:sp>` element."""
    shape_texts: list[str] = []
    for body in sp_elem:
        if not body.tag.endswith("txBody"):
            continue
        for para in body:
            if not para.tag.endswith("p"):
                continue
            line = "".join(_collect_element_texts(para)).strip()
            if line:
                shape_texts.append(line)
    return "\n".join(shape_texts)


def _render_openxml_slide(zf: zipfile.ZipFile, idx: int, slide_path: str) -> str:
    """Render one slide from OpenXML into a Markdown section block."""
    root = ET.fromstring(zf.read(slide_path))
    slide_lines: list[str] = [f"## Slide {idx}"]

    for elem in root.iter():
        if elem.tag.endswith("tbl"):
            md_table = _extract_openxml_table(elem)
            if md_table:
                slide_lines.append(md_table)
        elif elem.tag.endswith("sp"):
            text_block = _extract_openxml_shape_text(elem)
            if text_block:
                slide_lines.append(text_block)

    notes = _extract_slide_notes(zf, slide_path)
    if notes:
        slide_lines.append(f"> Notes: {notes}")

    return "\n\n".join(slide_lines) + "\n"


def _append_slide_within_budget(
    output_parts: list[str],
    slide_block: str,
    current_len: int,
    max_chars: int,
) -> tuple[int, bool]:
    """Append ``slide_block`` to ``output_parts`` or truncate when ``max_chars`` is reached."""
    if current_len + len(slide_block) > max_chars:
        remaining = max_chars - current_len
        if remaining > 0:
            output_parts.append(slide_block[:remaining])
        output_parts.append(
            f"\n\n[Content truncated at {max_chars} characters. "
            f"Specify slide numbers to read sections.]"
        )
        return current_len, True
    output_parts.append(slide_block)
    return current_len + len(slide_block), False


def _extract_via_openxml(
    path: Path, target_slides: tuple[int, ...] | None, max_chars: int
) -> str:
    """Extract presentation content using standard library zipfile and XML parser."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            ordered_slides = _get_ordered_slide_paths(zf)
            if not ordered_slides:
                return "[Empty presentation: no slides found]"

            total_slides = len(ordered_slides)
            slide_indices = set(target_slides) if target_slides is not None else None
            output_parts: list[str] = [f"# Presentation: {path.name} ({total_slides} slides)\n"]
            current_len = len(output_parts[0])

            for idx, slide_path in enumerate(ordered_slides, start=1):
                if slide_indices is not None and idx not in slide_indices:
                    continue
                slide_block = _render_openxml_slide(zf, idx, slide_path)
                current_len, stopped = _append_slide_within_budget(
                    output_parts, slide_block, current_len, max_chars
                )
                if stopped:
                    break

            return "\n".join(output_parts).strip()
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Corrupted or invalid PPTX file: {exc}") from exc


def _render_python_pptx_slide(slide: Any, idx: int) -> str:
    """Render one python-pptx slide object into a Markdown section block."""
    slide_lines: list[str] = [f"## Slide {idx}"]
    for shape in slide.shapes:
        if shape.has_text_frame:
            paragraphs: list[str] = []
            for para in shape.text_frame.paragraphs:
                cleaned = para.text.strip()
                if cleaned:
                    paragraphs.append(cleaned)
            if paragraphs:
                slide_lines.append("\n".join(paragraphs))
        elif shape.has_table:
            table_rows: list[list[str]] = []
            for row in shape.table.rows:
                table_rows.append([cell.text.strip() for cell in row.cells])
            md_table = _format_markdown_table(table_rows)
            if md_table:
                slide_lines.append(md_table)

    if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
        notes_text = slide.notes_slide.notes_text_frame.text.strip()
        if notes_text:
            slide_lines.append(f"> Notes: {notes_text}")

    return "\n\n".join(slide_lines) + "\n"


def _extract_via_python_pptx(
    path: Path, target_slides: tuple[int, ...] | None, max_chars: int
) -> str:
    """Extract presentation content using python-pptx when available."""
    try:
        import pptx  # type: ignore[import-untyped]  # pylint: disable=import-outside-toplevel
    except ImportError:
        return _extract_via_openxml(path, target_slides, max_chars)

    try:
        prs = pptx.Presentation(path)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("python-pptx failed to parse %s, falling back to openxml: %s", path, exc)
        return _extract_via_openxml(path, target_slides, max_chars)

    total_slides = len(prs.slides)
    if total_slides == 0:
        return "[Empty presentation: no slides found]"

    slide_indices = set(target_slides) if target_slides is not None else None
    output_parts: list[str] = [f"# Presentation: {path.name} ({total_slides} slides)\n"]
    current_len = len(output_parts[0])

    for idx, slide in enumerate(prs.slides, start=1):
        if slide_indices is not None and idx not in slide_indices:
            continue
        slide_block = _render_python_pptx_slide(slide, idx)
        current_len, stopped = _append_slide_within_budget(
            output_parts, slide_block, current_len, max_chars
        )
        if stopped:
            break

    return "\n".join(output_parts).strip()


def _read_pptx_sync(req: ReadPptxRequest) -> str:
    """Synchronous execution of PPTX extraction."""
    try:
        resolved_path = _resolve_pptx_path(req.pptx_path)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: Failed to resolve file path: {exc}"

    try:
        return _extract_via_python_pptx(resolved_path, req.slides, req.max_chars)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("Failed to read presentation %s", req.pptx_path)
        return f"[ERROR]: Failed to read PowerPoint presentation: {exc}"


@tool(
    name="read_pptx",
    description=(
        "Read PowerPoint (.pptx) presentation files into structured Markdown. "
        "Extracts slide titles, paragraphs, tables, and speaker notes. "
        "Supports filtering specific slides via slide numbers (e.g. 1, [1, 2], '1-3,5'). "
        "Input: file_path (local file path), optional slides, and max_chars limit."
    ),
)
async def read_pptx(
    file_path: str,
    slides: Any = None,
    max_chars: Any = DEFAULT_MAX_CHARS,
) -> str:
    """Async entrypoint for reading PowerPoint (.pptx) presentation files."""
    try:
        try:
            parsed_slides = _parse_slide_ranges(slides)
        except ValueError as exc:
            return f"[ERROR]: Invalid slides argument: {exc}"

        try:
            safe_max = int(max_chars)
        except (TypeError, ValueError):
            safe_max = DEFAULT_MAX_CHARS
        safe_max = max(1, min(safe_max, _MAX_CHARS_CEILING))

        req = ReadPptxRequest(
            pptx_path=file_path,
            slides=parsed_slides,
            max_chars=safe_max,
        )
        return await asyncio.to_thread(_read_pptx_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: read_pptx failed: {exc}"
