# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.pdf_tools import (
    DEFAULT_MAX_CHARS,
    _format_page_list,
    _normalize_request,
    _parse_page_ranges,
    read_pdf,
)


def _pdf_page_object(index: int, font_ref: int, with_stream: bool) -> bytes:
    """Serialize one /Page dictionary (stream object number is index-derived)."""
    contents = f" /Contents {4 + 2 * index} 0 R" if with_stream else ""
    return (
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        f" /Resources << /Font << /F1 {font_ref} 0 R >> >>{contents} >>"
    ).encode("latin-1")


def _pdf_stream_object(text: str | None) -> bytes:
    """Serialize a content-stream object showing ``text`` (empty when None)."""
    if text is None:
        payload = b""
    else:
        shown = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        payload = f"BT /F1 12 Tf 72 720 Td ({shown}) Tj ET".encode("latin-1")
    return f"<< /Length {len(payload)} >>\nstream\n".encode("latin-1") + payload + b"\nendstream"


def _build_minimal_pdf(pages: list[str | None]) -> bytes:
    """Hand-assemble a tiny valid PDF fixture, written from the PDF 1.4 spec.

    Each ``pages`` entry is that page's text; ``None`` produces a page with no
    content stream (no text layer). Base-14 Helvetica keeps the text
    extractable by pdfplumber/pdfminer without embedded fonts. The xref table
    is computed so the document is fully well-formed.
    """
    font_ref = 3 + 2 * len(pages)
    kid_refs = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))

    bodies: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kid_refs}] /Count {len(pages)} >>".encode("latin-1"),
    ]
    for i, text in enumerate(pages):
        bodies.append(_pdf_page_object(i, font_ref, with_stream=text is not None))
        bodies.append(_pdf_stream_object(text))
    bodies.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    chunks = [b"%PDF-1.4\n"]
    positions: list[int] = []
    cursor = len(chunks[0])
    for number, body in enumerate(bodies, start=1):
        positions.append(cursor)
        piece = f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
        chunks.append(piece)
        cursor += len(piece)

    # Cross-reference table: one fixed-width 20-byte line per object (spec 7.5.4).
    table_rows = ["0000000000 65535 f "]
    table_rows.extend(f"{position:010d} 00000 n " for position in positions)
    chunks.append(f"xref\n0 {len(table_rows)}\n".encode("latin-1"))
    chunks.append(("\n".join(table_rows) + "\n").encode("latin-1"))
    chunks.append(
        f"trailer\n<< /Size {len(table_rows)} /Root 1 0 R >>\n"
        f"startxref\n{cursor}\n%%EOF\n".encode("latin-1")
    )
    return b"".join(chunks)


def test_parse_page_ranges_variants():
    assert _parse_page_ranges(None) is None
    assert _parse_page_ranges("") is None
    assert _parse_page_ranges(3) == (3,)
    assert _parse_page_ranges("1-5") == (1, 2, 3, 4, 5)
    assert _parse_page_ranges("1,3,8-10") == (1, 3, 8, 9, 10)
    assert _parse_page_ranges("3, 1") == (1, 3)
    assert _parse_page_ranges([2, "4-5"]) == (2, 4, 5)


@pytest.mark.parametrize("bad", ["a", "5-2", "0", 0, -1, "1-", True])
def test_parse_page_ranges_rejects_invalid(bad):
    with pytest.raises(ValueError):
        _parse_page_ranges(bad)


def test_format_page_list_compresses_runs():
    assert _format_page_list([1, 2, 3, 7]) == "1-3,7"
    assert _format_page_list([5]) == "5"
    assert _format_page_list([3, 1, 2, 2]) == "1-3"
    assert _format_page_list([]) == ""


def test_normalize_request_defaults_and_clamping():
    req = _normalize_request({"pdf_path": "/tmp/a.pdf"})
    assert req.pages is None
    assert req.max_chars == DEFAULT_MAX_CHARS

    req = _normalize_request({"pdf_path": "/tmp/a.pdf", "pages": "2-3", "max_chars": 10})
    assert req.pages == (2, 3)
    assert req.max_chars == 1_000  # floor

    req = _normalize_request({"pdf_path": "/tmp/a.pdf", "max_chars": 10**9})
    assert req.max_chars == 200_000  # ceiling

    with pytest.raises(ValueError):
        _normalize_request({})


@pytest.mark.asyncio
async def test_read_pdf_extracts_pages_and_flags_blank(tmp_path: Path):
    pytest.importorskip("pdfplumber")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(
        _build_minimal_pdf(["Hello page one", "Second page text", None])
    )

    result = await read_pdf.invoke({"inputs": {"pdf_path": str(pdf_path)}})
    assert "total pages: 3" in result
    assert "--- Page 1 ---" in result
    assert "Hello page one" in result
    assert "Second page text" in result
    assert "no text layer" in result
    assert "Pages without extractable text: 3" in result


@pytest.mark.asyncio
async def test_read_pdf_respects_page_selection(tmp_path: Path):
    pytest.importorskip("pdfplumber")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(_build_minimal_pdf(["Alpha", "Bravo", "Charlie"]))

    result = await read_pdf.invoke({"inputs": {"pdf_path": str(pdf_path), "pages": "2"}})
    assert "Bravo" in result
    assert "Alpha" not in result
    assert "Charlie" not in result

    result = await read_pdf.invoke({"inputs": {"pdf_path": str(pdf_path), "pages": "2,9"}})
    assert "Bravo" in result
    assert "exceed" in result  # out-of-range note for page 9


@pytest.mark.asyncio
async def test_read_pdf_truncates_at_max_chars(tmp_path: Path):
    pytest.importorskip("pdfplumber")
    long_text = "word " * 500  # ~2500 chars on one page
    pdf_path = tmp_path / "long.pdf"
    pdf_path.write_bytes(_build_minimal_pdf([long_text.strip(), "Tail page"]))

    result = await read_pdf.invoke(
        {"inputs": {"pdf_path": str(pdf_path), "max_chars": 1000}}
    )
    assert "truncated at max_chars" in result
    assert "Tail page" not in result
    # Truncation must list the unread pages so the model can continue in chunks
    assert "unread pages: 2" in result


@pytest.mark.asyncio
async def test_read_pdf_relative_path_anchors_to_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pytest.importorskip("pdfplumber")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "docs").mkdir()
    (workspace / "docs" / "note.pdf").write_bytes(_build_minimal_pdf(["Workspace anchored"]))
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.pdf_tools.get_agent_workspace_dir",
        lambda: workspace,
    )

    result = await read_pdf.invoke({"inputs": {"pdf_path": "docs/note.pdf"}})
    assert "Workspace anchored" in result


@pytest.mark.asyncio
async def test_read_pdf_error_paths(tmp_path: Path):
    result = await read_pdf.invoke({"inputs": {"pdf_path": str(tmp_path / "missing.pdf")}})
    assert result.startswith("[ERROR]")

    not_pdf = tmp_path / "note.txt"
    not_pdf.write_text("hi", encoding="utf-8")
    result = await read_pdf.invoke({"inputs": {"pdf_path": str(not_pdf)}})
    assert result.startswith("[ERROR]")
    assert "only accepts .pdf" in result

    result = await read_pdf.invoke({"inputs": {}})
    assert result.startswith("[ERROR]")
