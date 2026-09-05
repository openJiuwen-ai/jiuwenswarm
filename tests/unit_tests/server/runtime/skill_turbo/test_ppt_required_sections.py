# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.content_plan import (
    ContentPlanError,
    _validate_outline_markdown_basic,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.intent_classify import (
    _parse_slots_from_llm_response,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import (
    PptCommon,
)


def _required_sections() -> list[dict[str, str]]:
    return [
        {"title": "标题页", "page_type": "cover"},
        {"title": "目录", "page_type": "agenda"},
        {"title": "市场概况", "page_type": "content"},
        {"title": "竞争格局", "page_type": "content"},
        {"title": "技术趋势", "page_type": "content"},
        {"title": "展望结论", "page_type": "ending"},
    ]


def _outline_page(page_number: int, title: str, page_type: str, research: bool) -> str:
    return f"""### P{page_number}: {title}
- **类型**：{page_type}
- **研究需求**：{"✅" if research else "❌"}
- **标题**：{title}
- **内容概要**：测试内容
- **研究查询**：测试查询
- **数据需求**：无
"""


def _validate_required_sections(
    pages: list[str],
    required_sections: list[dict[str, str]],
    *,
    structural_page_request: str = "none",
) -> None:
    outline = "# 大纲：测试主题\n\n## 页面规划\n\n" + "\n".join(pages)
    _validate_outline_markdown_basic(
        outline,
        topic="测试主题",
        page_count=None,
        structural_page_request=structural_page_request,
        required_sections=required_sections,
    )


def test_required_sections_override_conflicting_total_page_count() -> None:
    inputs = {
        "page_count": 1,
        "requested_total_pages": 3,
        "structural_page_request": "none",
        "required_sections": _required_sections(),
    }

    PptCommon.resolve_required_section_budget(inputs)

    assert inputs["page_count"] == 3
    assert inputs["structural_page_request"] == "agenda"
    assert inputs["structural_page_count"] == 1
    assert inputs["resolved_total_pages"] == 6
    assert inputs["required_agenda_item_count"] == 4
    assert inputs["page_count_resolution"] == "required_sections_override"


def test_required_sections_do_not_shrink_larger_page_budget() -> None:
    inputs = {
        "page_count": 7,
        "requested_total_pages": 10,
        "structural_page_request": "none",
        "required_sections": _required_sections(),
    }

    PptCommon.resolve_required_section_budget(inputs)

    assert inputs["page_count"] == 7
    assert inputs["resolved_total_pages"] == 10
    assert inputs["page_count_resolution"] == "required_sections_fit"


def test_intent_parser_preserves_structured_required_sections() -> None:
    response = json.dumps({
        "doc_paths": [],
        "slots": {
            "topic": "2025年新能源汽车市场趋势",
            "page_count": 1,
            "audience": "普通受众",
            "presentation_purpose": "教学分享",
            "style_id": "custom",
            "pack_dir": "",
            "requested_total_pages": 3,
            "required_sections": _required_sections(),
        },
    }, ensure_ascii=False)

    slots = _parse_slots_from_llm_response(response)

    assert slots["requested_total_pages"] == 3
    assert slots["required_sections"] == _required_sections()


def test_required_sections_cannot_reuse_one_outline_page() -> None:
    pages = [
        _outline_page(1, "标题页", "cover", False),
        _outline_page(2, "市场概况", "content", True),
        _outline_page(3, "展望结论", "ending", False),
    ]
    required_sections = [
        {"title": "市场概况", "page_type": "content"},
        {"title": "市场概况", "page_type": "content"},
    ]

    with pytest.raises(ContentPlanError, match="未按指定页型落实章节"):
        _validate_required_sections(pages, required_sections)


def test_required_section_title_does_not_use_substring_match() -> None:
    pages = [
        _outline_page(1, "标题页", "cover", False),
        _outline_page(2, "市场规模分析", "content", True),
        _outline_page(3, "展望结论", "ending", False),
    ]
    required_sections = [{"title": "市场", "page_type": "content"}]

    with pytest.raises(ContentPlanError, match="未按指定页型落实章节"):
        _validate_required_sections(pages, required_sections)


def test_transition_page_does_not_satisfy_required_ending() -> None:
    pages = [
        _outline_page(1, "标题页", "cover", False),
        _outline_page(2, "展望结论", "transition", False),
        _outline_page(3, "结束页", "ending", False),
    ]
    required_sections = [{"title": "展望结论", "page_type": "ending"}]

    with pytest.raises(ContentPlanError, match="未按指定页型落实章节"):
        _validate_required_sections(
            pages,
            required_sections,
            structural_page_request="transition",
        )
