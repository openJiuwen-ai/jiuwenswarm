# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Batch C: page_count structural deduction + outline contract normalize/total pages."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import content_plan as cp
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import intent_classify as ic
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import requirement_collect as rc


def _page_block(
    num: int,
    *,
    page_type: str,
    research: str,
    title: str,
    summary: str = "概要内容足够长",
    queries: str = "查询A；查询B",
    needs: str = "需要指标数据",
) -> str:
    return (
        f"### P{num}: {title}\n"
        f"- **类型**: {page_type}\n"
        f"- **研究需求**: {research}\n"
        f"- **标题**: {title}\n"
        f"- **内容概要**: {summary}\n"
        f"- **研究查询**: {queries}\n"
        f"- **数据需求**: {needs}\n"
    )


def _minimal_outline(*, with_agenda: bool = False, topic: str = "AI 助手") -> str:
    pages = [_page_block(1, page_type="cover", research="❌", title="封面", queries="-", needs="-")]
    n = 2
    if with_agenda:
        pages.append(
            _page_block(n, page_type="agenda", research="❌", title="目录", queries="-", needs="-")
        )
        n += 1
    for i in range(4):
        pages.append(
            _page_block(
                n + i,
                page_type="content",
                research="✅",
                title=f"内容{i+1}",
            )
        )
    end_n = n + 4
    pages.append(
        _page_block(end_n, page_type="ending", research="❌", title="结束", queries="-", needs="-")
    )
    body = "\n".join(pages)
    return f"# 大纲：{topic}\n\n## 页面规划\n\n{body}\n"


def test_intent_and_requirement_prompts_deduct_structural_pages():
    # 意见 9：未指定结构数量时禁止 LLM 自扣/试算 ceil；只扣封面结束，余下由系统反推。
    assert "max(N - 2, 1)" in ic._LLM_PATH_AND_SLOTS_SYSTEM_PROMPT
    assert "禁止自行扣减结构页" in ic._LLM_PATH_AND_SLOTS_SYSTEM_PROMPT
    assert "outline-planner" in ic._LLM_PATH_AND_SLOTS_SYSTEM_PROMPT
    assert "max(N - 2 - 结构页扣减, 1)" not in ic._LLM_PATH_AND_SLOTS_SYSTEM_PROMPT

    assert "page_count_basis" in rc._P21_SLOT_SYSTEM_PROMPT
    assert "max(N - 2, 1)" in rc._P21_SLOT_SYSTEM_PROMPT
    assert "ceil" in rc._P21_SLOT_SYSTEM_PROMPT
    assert "outline-planner" in rc._P21_SLOT_SYSTEM_PROMPT
    assert "中间结构页数" in rc._P21_SLOT_SYSTEM_PROMPT
    assert "max(N - 2 - 结构页扣减, 1)" not in rc._P21_SLOT_SYSTEM_PROMPT


def test_resolve_mid_structural_matches_outline_planner_default():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

    assert PptCommon.resolve_mid_structural_page_count(7, "section", None) == 2
    assert PptCommon.resolve_mid_structural_page_count(4, "section", None) == 1
    assert PptCommon.resolve_mid_structural_page_count(8, "agenda", None) == 1
    assert PptCommon.content_pages_from_total_pages(
        10, structural_page_request="section",
    ) == 6


def test_validate_outline_total_pages_with_agenda():
    text = _minimal_outline(with_agenda=True)
    # 4 content + cover/ending 2 + agenda 1 = 7
    cp._validate_outline_markdown_basic(
        text,
        topic="AI 助手",
        page_count=4,
        structural_page_request="agenda",
        structural_page_count=1,
    )


def test_validate_outline_total_pages_mismatch_raises():
    text = _minimal_outline(with_agenda=True)
    with pytest.raises(cp.ContentPlanError, match="总页数应为"):
        cp._validate_outline_markdown_basic(
            text,
            topic="AI 助手",
            page_count=4,
            structural_page_request="none",
            structural_page_count=None,
        )


def test_normalize_outline_contract_fixes_title_and_sources_and_placeholders():
    raw = (
        "# 大纲：旧标题\n\n"
        "## 页面规划\n\n"
        + _page_block(
            1, page_type="cover", research="❌", title="封面", queries="-", needs="-"
        )
        + _page_block(
            2,
            page_type="content",
            research="✅",
            title="正文",
            queries="-",
            needs="N/A",
        )
        + _page_block(
            3, page_type="ending", research="❌", title="结束", queries="-", needs="-"
        )
    )
    inputs = {
        "topic": "新主题",
        "search_mode": "force_search",
        "p4_quick_research_status": "completed",
        "search_results": [
            {
                "dimension": "现状",
                "query": "AI 助手 市场",
                "result": "见 https://example.com/report 详情",
            }
        ],
        "p4_search_queries": [{"query": "AI 助手 市场规模 报告"}],
        "page_count": 1,
        "structural_page_request": "none",
    }
    out = cp._normalize_outline_contract(raw, inputs)
    assert "# 大纲：新主题" in out
    assert "## 已搜索来源" in out
    assert "https://example.com/report" in out
    assert "AI 助手 市场规模 报告" in out
    assert "**数据需求**: 概要内容足够长" in out


def test_normalize_then_full_validate_passes_for_fixed_outline():
    raw = (
        "# 大纲：错题\n\n"
        "## 页面规划\n\n"
        + _page_block(1, page_type="cover", research="❌", title="封面", queries="-", needs="-")
        + _page_block(
            2,
            page_type="content",
            research="✅",
            title="市场",
            queries="-",
            needs="N/A",
        )
        + _page_block(3, page_type="ending", research="❌", title="结束", queries="-", needs="-")
    )
    inputs = {
        "topic": "市场分析",
        "page_count": 1,
        "search_mode": "no_search",
        "structural_page_request": "none",
        "p4_search_queries": [{"query": "市场分析 关键数据"}],
    }
    normalized = cp._normalize_outline_contract(raw, inputs)
    err = cp._outline_full_error(normalized, inputs)
    assert err is None
