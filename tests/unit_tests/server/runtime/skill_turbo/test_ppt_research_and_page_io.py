# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for research schema alignment and page file IO isolation."""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import deep_research as dr
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import (
    is_write_protocol_error,
    safe_overwrite_file_impl,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    PPTPageGenNode,
    _is_slide_exportable,
)


def test_research_page_skeleton_uses_new_slots():
    skeleton = dr._RESEARCH_PAGE_SKELETON
    for required in ("主轴", "关键数据", "上屏要点", "案例", "来源留痕"):
        assert required in skeleton
    for forbidden in ("关键数据清单", "时序数据", "对比数据", "案例素材"):
        # 骨架正文可提到禁止用旧槽位名，但不得把它们列为顶层 `- **旧名**` 槽位
        assert f"- **{forbidden}**" not in skeleton


def test_fallback_and_no_data_sections_use_new_slots():
    page = {
        "page_number": 3,
        "title": "测试页",
        "page_type": "data",
        "data_needs": ["市场规模"],
        "research_queries": ["query1"],
    }
    worker = dr.PageWorkerNode()
    fallback = worker._build_fallback_page_section(page)
    assert "主轴" in fallback
    assert "关键数据" in fallback
    assert "上屏要点" in fallback
    assert "#### 来源留痕" in fallback
    assert "关键数据清单" not in fallback

    stub = worker._build_no_data_page_section(page, "topic", "auto", "L1")
    assert "主轴" in stub
    assert "关键数据" in stub
    assert "上屏要点" in stub
    assert "#### 来源留痕" in stub
    assert "关键数据清单" not in stub


def test_parse_validate_research_output_extracts_invalid_pages():
    detail = """Exit code 1
{
  "ok": false,
  "summary": {"invalid": [3, 4], "passed": []},
  "pages": {
    "3": {"ok": false, "reasons": ["missing:主轴", "missing:上屏要点"]},
    "4": {"ok": false, "reasons": ["missing:来源留痕"]}
  }
}
"""
    invalid, reasons = dr.PageWorkerNode._parse_validate_research_output(detail)
    assert invalid == [3, 4]
    assert reasons[3] == ["missing:主轴", "missing:上屏要点"]
    assert reasons[4] == ["missing:来源留痕"]


def test_is_write_protocol_error():
    assert is_write_protocol_error(
        Exception("File has been modified since read, either by the user or by a linter.")
    )
    assert is_write_protocol_error(
        Exception("File has not been fully read yet. You have read 0 of 0 lines.")
    )
    assert not is_write_protocol_error(Exception("disk full"))


class _FakeNode:
    def __init__(self) -> None:
        self.reads = 0
        self.writes = 0
        self.fail_protocol_once = True
        self.tools = {"read_file", "write_file"}

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        if name == "read_file":
            self.reads += 1
            return {"content": "<html></html>"}
        if name == "write_file":
            self.writes += 1
            if self.fail_protocol_once:
                self.fail_protocol_once = False
                raise RuntimeError("File has been modified since read")
            return {"ok": True}
        raise AssertionError(f"unexpected tool {name}")


@pytest.mark.asyncio
async def test_safe_overwrite_retries_protocol_error():
    node = _FakeNode()
    ok = await safe_overwrite_file_impl(
        node,
        r"D:\tmp\page-1.pptx.html",
        "<html>ok</html>",
        log_prefix="[test]",
    )
    assert ok is True
    assert node.writes == 2
    assert node.reads >= 2


def test_is_slide_exportable_requires_main_in_ppt_slide():
    bad = "<html><body><main>x</main></body></html>"
    good = (
        '<html><body><div class="ppt-slide"><main class="flex-1">'
        "<section>a</section></main></div></body></html>"
    )
    assert _is_slide_exportable(bad) is False
    assert _is_slide_exportable(good) is True


@pytest.mark.asyncio
async def test_reconcile_missing_pages_clears_false_positives(tmp_path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    html = (
        '<!DOCTYPE html><html><body>'
        '<div class="ppt-slide h-[720px]">'
        '<main class="flex-1"><section>content</section></main>'
        "</div></body></html>"
    )
    for i in range(1, 4):
        (pages_dir / f"page-{i}.pptx.html").write_text(html, encoding="utf-8")

    node = PPTPageGenNode()

    async def _read_file(path: str) -> str:
        from pathlib import Path

        p = Path(path)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8")

    node._read_file = _read_file  # type: ignore[method-assign]
    missing, files = await node._reconcile_missing_pages(
        pages_dir=str(pages_dir),
        total_pages=3,
        reported_missing=[1, 2, 3],
        reported_page_files=[],
    )
    assert missing == []
    assert files == [
        "page-1.pptx.html",
        "page-2.pptx.html",
        "page-3.pptx.html",
    ]
