"""Source report handling for evidence refreshes of brief reports."""

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch.brief_refresh import (
    build_brief_refresh_query,
)


def test_refresh_query_includes_report_evidence_and_reference_urls(tmp_path):
    report = tmp_path / "brief.html"
    source = (
        '<html><head><style>hidden-style</style></head><body>'
        '<h1>原报告</h1><p>待补充的结论：42%</p>'
        '<a href="https://example.org/source">原始参考</a>'
        '<script>hidden-script</script></body></html>'
    )
    report.write_text(source, encoding="utf-8")

    query = build_brief_refresh_query("研究用户需求", str(report), "补充参考来源")

    assert "研究用户需求" in query
    assert "补充参考来源" in query
    assert "待补充的结论：42%" in query
    assert "https://example.org/source" in query
    assert "hidden-script" not in query
    assert "hidden-style" not in query
    assert "精简版" in query
    assert "专业版" in query
    assert report.read_text(encoding="utf-8") == source


@pytest.mark.parametrize("content", ["", "<html><script>only code</script></html>"])
def test_refresh_rejects_empty_report(tmp_path, content):
    report = tmp_path / "brief.html"
    report.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        build_brief_refresh_query("task", str(report), "evidence")


def test_refresh_rejects_missing_or_non_file_sources(tmp_path):
    with pytest.raises(OSError):
        build_brief_refresh_query("task", str(tmp_path / "missing.html"), "evidence")
    with pytest.raises(ValueError, match="regular file"):
        build_brief_refresh_query("task", str(tmp_path), "evidence")


@pytest.mark.parametrize("file_size", [1024 * 1024 + 1, 32 * 1024 * 1024])
def test_refresh_accepts_large_html_with_bounded_visible_text(tmp_path, file_size):
    report = tmp_path / "brief.html"
    prefix = b"<html><head><script>"
    suffix = b"</script></head><body><p>Original report evidence</p></body></html>"
    script_size = file_size - len(prefix) - len(suffix)
    report.write_bytes(prefix + b"x" * script_size + suffix)

    query = build_brief_refresh_query("original topic", str(report), "supplement evidence")

    assert "Original report evidence" in query
    assert "original topic" in query
    assert "supplement evidence" in query
    assert "x" * 100 not in query
    assert len(query) < 2000
    assert report.stat().st_size == file_size


def test_refresh_rejects_oversized_report_without_truncating(tmp_path):
    report = tmp_path / "brief.html"
    file_size = 32 * 1024 * 1024 + 1
    with report.open("wb") as stream:
        stream.truncate(file_size)
    with pytest.raises(ValueError, match="^brief source is too large$"):
        build_brief_refresh_query("task", str(report), "evidence")
    assert report.stat().st_size == file_size


def test_refresh_keeps_visible_text_limit_for_allowed_size_html(tmp_path):
    report = tmp_path / "brief.html"
    report.write_text("<html><body>" + "x" * 128_001 + "</body></html>", encoding="utf-8")
    with pytest.raises(ValueError, match="^brief source text is too large$"):
        build_brief_refresh_query("task", str(report), "evidence")
