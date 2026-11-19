# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for PowerPoint reading tool (read_pptx)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools.pptx_tools import (
    _format_markdown_table,
    _extract_slide_notes,
    _parse_slide_ranges,
    read_pptx,
)


def _create_mock_pptx(dest_path: Path, slide_count: int = 2) -> None:
    """Create a minimal valid OpenXML PPTX file for unit testing."""
    with zipfile.ZipFile(dest_path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>",
        )
        for i in range(1, slide_count + 1):
            slide_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:sp>
        <p:txBody>
          <a:p><a:r><a:t>Slide {i} Heading</a:t></a:r></a:p>
          <a:p><a:r><a:t>Content point for slide {i}</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <p:graphicFrame>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">
            <a:tbl>
              <a:tr>
                <a:tc><a:txBody><a:p><a:r><a:t>Col A</a:t></a:r></a:p></a:txBody></a:tc>
                <a:tc><a:txBody><a:p><a:r><a:t>Col B</a:t></a:r></a:p></a:txBody></a:tc>
              </a:tr>
              <a:tr>
                <a:tc><a:txBody><a:p><a:r><a:t>Val {i}-1</a:t></a:r></a:p></a:txBody></a:tc>
                <a:tc><a:txBody><a:p><a:r><a:t>Val {i}-2</a:t></a:r></a:p></a:txBody></a:tc>
              </a:tr>
            </a:tbl>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>
    </p:spTree>
  </p:cSld>
</p:sld>"""
            zf.writestr(f"ppt/slides/slide{i}.xml", slide_xml)


def test_parse_slide_ranges() -> None:
    """Test parsing slide ranges with varied inputs."""
    assert _parse_slide_ranges(None) is None
    assert _parse_slide_ranges("") is None
    assert _parse_slide_ranges(1) == (1,)
    assert _parse_slide_ranges([1, 3, 2]) == (1, 2, 3)
    assert _parse_slide_ranges("1-3, 5") == (1, 2, 3, 5)

    with pytest.raises(ValueError, match="slides must be"):
        _parse_slide_ranges(True)

    with pytest.raises(ValueError, match="1-based"):
        _parse_slide_ranges(0)

    with pytest.raises(ValueError, match="Invalid slide range"):
        _parse_slide_ranges("abc-def")

    with pytest.raises(ValueError, match="Slide range span too large"):
        _parse_slide_ranges("1-10000000")


def test_format_markdown_table() -> None:
    """Test formatting 2D lists into Markdown tables."""
    assert _format_markdown_table([]) == ""
    assert _format_markdown_table([[]]) == ""
    rows = [["Name", "Role"], ["Alice", "Engineer"]]
    table = _format_markdown_table(rows)
    assert "| Name | Role |" in table
    assert "| --- | --- |" in table
    assert "| Alice | Engineer |" in table


@pytest.mark.asyncio
async def test_read_pptx_full(tmp_path: Path) -> None:
    """Test reading all slides from a presentation."""
    pptx_path = tmp_path / "presentation.pptx"
    _create_mock_pptx(pptx_path, slide_count=2)

    result = await read_pptx.invoke({"file_path": str(pptx_path)})
    assert "# Presentation: presentation.pptx" in result
    assert "## Slide 1" in result
    assert "Slide 1 Heading" in result
    assert "| Col A | Col B |" in result
    assert "## Slide 2" in result
    assert "Slide 2 Heading" in result


@pytest.mark.asyncio
async def test_read_pptx_filter_slides(tmp_path: Path) -> None:
    """Test reading specific slides via slides argument."""
    pptx_path = tmp_path / "sample.pptx"
    _create_mock_pptx(pptx_path, slide_count=3)

    result_range = await read_pptx.invoke({"file_path": str(pptx_path), "slides": "2-3"})
    assert "## Slide 1" not in result_range
    assert "## Slide 2" in result_range
    assert "## Slide 3" in result_range

    result_single = await read_pptx.invoke({"file_path": str(pptx_path), "slides": 2})
    assert "## Slide 1" not in result_single
    assert "## Slide 2" in result_single
    assert "## Slide 3" not in result_single


@pytest.mark.asyncio
async def test_read_pptx_truncation(tmp_path: Path) -> None:
    """Test character limit truncation."""
    pptx_path = tmp_path / "long.pptx"
    _create_mock_pptx(pptx_path, slide_count=2)

    result = await read_pptx.invoke({"file_path": str(pptx_path), "max_chars": 80})
    assert "Content truncated at 80 characters" in result


@pytest.mark.asyncio
async def test_read_pptx_empty(tmp_path: Path) -> None:
    """Test reading an empty presentation without slides."""
    pptx_path = tmp_path / "empty.pptx"
    with zipfile.ZipFile(pptx_path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")

    result = await read_pptx.invoke({"file_path": str(pptx_path)})
    assert "[Empty presentation: no slides found]" in result


@pytest.mark.asyncio
async def test_read_pptx_notes_extraction(tmp_path: Path) -> None:
    """Test speaker notes extraction in OpenXML fallback."""
    pptx_path = tmp_path / "notes_sample.pptx"
    _create_mock_pptx(pptx_path, slide_count=1)

    notes_xml = """<?xml version="1.0" encoding="UTF-8"?>
<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
         xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>Remember to mention quarterly growth targets</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:notes>"""
    with zipfile.ZipFile(pptx_path, "a") as zf:
        zf.writestr("ppt/notesSlides/notesSlide1.xml", notes_xml)

    result = await read_pptx.invoke({"file_path": str(pptx_path)})
    assert "> Notes: Remember to mention quarterly growth targets" in result


def _notes_xml(lines: list[str]) -> str:
    """Build a notes slide XML containing the given paragraphs."""
    paras = "".join(f"<a:p><a:r><a:t>{ln}</a:t></a:r></a:p>" for ln in lines)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        f"<p:cSld><p:spTree><p:sp><p:txBody>{paras}</p:txBody></p:sp></p:spTree></p:cSld>"
        "</p:notes>"
    )


def test_extract_slide_notes_no_duplicates(tmp_path: Path) -> None:
    """Two note paragraphs must yield exactly two lines."""
    pptx_path = tmp_path / "dup.pptx"
    _create_mock_pptx(pptx_path, slide_count=1)
    with zipfile.ZipFile(pptx_path, "a") as zf:
        zf.writestr("ppt/notesSlides/notesSlide1.xml", _notes_xml(["First note", "Second note"]))
    with zipfile.ZipFile(pptx_path) as zf:
        notes = _extract_slide_notes(zf, "ppt/slides/slide1.xml")
    assert notes.split("\n") == ["First note", "Second note"]


def test_extract_slide_notes_follows_rels_after_reorder(tmp_path: Path) -> None:
    """Notes are resolved through slide rels, not presentation order."""
    pptx_path = tmp_path / "reorder.pptx"
    _create_mock_pptx(pptx_path, slide_count=2)
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    type_pre = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pres_rels = (
        f'<Relationships xmlns="{rel_ns}">'
        f'<Relationship Id="rId1" Type="{type_pre}/slide" Target="slides/slide1.xml"/>'
        f'<Relationship Id="rId2" Type="{type_pre}/slide" Target="slides/slide2.xml"/>'
        "</Relationships>"
    )
    pres = (
        f'<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        f'xmlns:r="{type_pre}"><p:sldIdLst>'
        '<p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/>'
        "</p:sldIdLst></p:presentation>"
    )
    with zipfile.ZipFile(pptx_path, "a") as zf:
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        zf.writestr("ppt/presentation.xml", pres)
        for num in (1, 2):
            zf.writestr(
                f"ppt/slides/_rels/slide{num}.xml.rels",
                f'<Relationships xmlns="{rel_ns}"><Relationship Id="rId1" '
                f'Type="{type_pre}/notesSlide" Target="../notesSlides/notesSlide{num}.xml"/>'
                "</Relationships>",
            )
            note_xml = _notes_xml([f"Note for slide{num}"])
            zf.writestr(f"ppt/notesSlides/notesSlide{num}.xml", note_xml)
    with zipfile.ZipFile(pptx_path) as zf:
        assert _extract_slide_notes(zf, "ppt/slides/slide2.xml") == "Note for slide2"
        assert _extract_slide_notes(zf, "ppt/slides/slide1.xml") == "Note for slide1"


@pytest.mark.asyncio
async def test_read_pptx_expanduser(tmp_path: Path) -> None:
    """Test path expanduser handling."""
    pptx_path = tmp_path / "user_sample.pptx"
    _create_mock_pptx(pptx_path, slide_count=1)

    with patch.object(Path, "expanduser", return_value=pptx_path):
        result = await read_pptx.invoke({"file_path": "~/user_sample.pptx"})
        assert "Slide 1 Heading" in result


@pytest.mark.asyncio
async def test_read_pptx_invalid_extension(tmp_path: Path) -> None:
    """Test unsupported file extensions."""
    invalid_file = tmp_path / "sample.txt"
    invalid_file.write_text("not a pptx", encoding="utf-8")

    result = await read_pptx.invoke({"file_path": str(invalid_file)})
    assert "[ERROR]" in result
    assert "Unsupported file format" in result


@pytest.mark.asyncio
async def test_read_pptx_file_not_found(tmp_path: Path) -> None:
    """Test missing file error handling."""
    missing = tmp_path / "non_existent.pptx"
    result = await read_pptx.invoke({"file_path": str(missing)})
    assert "[ERROR]" in result
    assert "File not found" in result


@pytest.mark.asyncio
async def test_read_pptx_corrupted_file(tmp_path: Path) -> None:
    """Test corrupted zip file handling."""
    corrupted = tmp_path / "corrupted.pptx"
    corrupted.write_bytes(b"not a valid zip file stream")

    result = await read_pptx.invoke({"file_path": str(corrupted)})
    assert "[ERROR]" in result
    assert "Corrupted or invalid PPTX file" in result


@pytest.mark.asyncio
async def test_read_pptx_workspace_escape_rejected(tmp_path: Path) -> None:
    """Test rejection of paths escaping workspace boundaries."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    with patch(
        "jiuwenswarm.agents.harness.common.tools.pptx_tools._get_workspace",
        return_value=str(ws),
    ):
        result = await read_pptx.invoke({"file_path": "../../escaped.pptx"})
        assert "[ERROR]" in result
        assert "Path must be inside workspace" in result


@pytest.mark.asyncio
async def test_read_pptx_absolute_path_allowed(tmp_path: Path) -> None:
    """Test that absolute path outside workspace can be read directly."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside_file = tmp_path / "outside.pptx"
    _create_mock_pptx(outside_file, slide_count=1)

    with patch(
        "jiuwenswarm.agents.harness.common.tools.pptx_tools._get_workspace",
        return_value=str(ws),
    ):
        result = await read_pptx.invoke({"file_path": str(outside_file)})
        assert "# Presentation: outside.pptx" in result
        assert "Slide 1 Heading" in result


@pytest.mark.asyncio
async def test_read_pptx_invalid_max_chars_fallback(tmp_path: Path) -> None:
    """Test non-int max_chars fallback to default safely without raising TypeError."""
    pptx_path = tmp_path / "sample.pptx"
    _create_mock_pptx(pptx_path, slide_count=1)

    result = await read_pptx.invoke({"file_path": str(pptx_path), "max_chars": "invalid"})
    assert "# Presentation: sample.pptx" in result
    assert "Slide 1 Heading" in result


@pytest.mark.asyncio
async def test_read_pptx_relative_path_without_workspace() -> None:
    """Test relative path rejection when runtime workspace is unavailable."""
    with patch(
        "jiuwenswarm.agents.harness.common.tools.pptx_tools._get_workspace",
        return_value=None,
    ):
        result = await read_pptx.invoke({"file_path": "relative/file.pptx"})
        assert "[ERROR]" in result
        assert "runtime workspace is unavailable" in result
