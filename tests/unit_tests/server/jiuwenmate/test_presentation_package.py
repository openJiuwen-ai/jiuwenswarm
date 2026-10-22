"""A presentation whose declared slide size disagrees with the page can be opened."""

from __future__ import annotations

import zipfile

from jiuwenswarm.server.jiuwenmate.presentation_package import normalize_presentation_package


def _package(path, slide_size: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as package:
        package.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            f"{slide_size}<p:notesSz cx=\"6858000\" cy=\"9144000\"/>"
            "</p:presentation>",
        )
        package.writestr("[Content_Types].xml", "<Types/>")


def _slide_size(path) -> str:
    xml = zipfile.ZipFile(path).read("ppt/presentation.xml").decode("utf-8")
    start = xml.index("<p:sldSz")
    return xml[start : xml.index("/>", start) + 2]


def test_rounded_widescreen_is_snapped_to_the_standard_16_by_9_page(tmp_path):
    path = tmp_path / "workbuddy_intro.pptx"
    _package(path, '<p:sldSz cx="12191695" cy="6858000" type="screen4x3"/>')

    assert normalize_presentation_package(path) is True
    assert _slide_size(path) == '<p:sldSz cx="12192000" cy="6858000" type="screen16x9"/>'
    assert 'notesSz cx="6858000"' in zipfile.ZipFile(path).read("ppt/presentation.xml").decode("utf-8")
    assert normalize_presentation_package(path) is False


def test_a_nonstandard_page_is_marked_custom_without_resizing(tmp_path):
    path = tmp_path / "custom.pptx"
    _package(path, '<p:sldSz cx="8000000" cy="5000000" type="screen4x3"/>')

    assert normalize_presentation_package(path) is True
    assert _slide_size(path) == '<p:sldSz cx="8000000" cy="5000000" type="custom"/>'


def test_a_matching_standard_page_is_left_unchanged(tmp_path):
    path = tmp_path / "standard.pptx"
    _package(path, '<p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>')

    assert normalize_presentation_package(path) is False
    assert normalize_presentation_package(tmp_path / "notes.md") is False
