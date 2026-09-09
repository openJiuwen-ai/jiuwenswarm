# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for P0 content_plan and layout alignment changes."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import content_plan as cp
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import resolve_layout_density
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    PPTPageGenNode,
    _build_layout_patch_prompt,
    _layout_fix_hint_from_cli_output,
    _post_check_layout_hints,
)


def test_user_context_search_terms_prefers_dimensions_and_structure():
    inputs = {
        "topic": "AI 助手",
        "user_dimensions": ["核心功能", "应用场景"],
        "user_structure": "封面、目录、功能介绍、结束页",
    }
    terms = cp._user_context_search_terms(inputs)
    assert "核心功能" in terms
    assert "应用场景" in terms
    assert "功能介绍" in terms
    assert "封面" not in terms
    assert "AI 助手" in terms


def test_results_cover_user_context_by_topic_keyword():
    inputs = {"topic": "OpenClaw 产品", "user_dimensions": [], "user_structure": ""}
    batches = [
        {
            "query": "test",
            "result": "Query: test\nOpenClaw 产品是一款面向开发者的 AI 助手平台。",
        }
    ]
    assert cp._results_cover_user_context(inputs, batches)


def test_resolve_layout_density_matches_cli_annotation():
    assert resolve_layout_density(None) == "lean"
    assert resolve_layout_density("") == "standard"
    assert resolve_layout_density("数据有限，基于用户素材整理。") == "lean"
    assert resolve_layout_density("市场规模达到 100 亿。") == "standard"


def test_post_check_layout_hints_do_not_block_on_grid():
    html = '<div class="grid grid-cols-2"><p>内容</p></div>'
    hints = _post_check_layout_hints(html)
    assert "Grid" not in " ".join(hints)
    assert "overflow-hidden" not in " ".join(hints)


def test_layout_fix_hint_from_cli_output_maps_designer_fixes():
    cli_text = "[overflow] block foo\n[v-gap] card bar\n[whitespace] main"
    hint = _layout_fix_hint_from_cli_output(cli_text)
    assert "overflow" in hint
    assert "v-gap" in hint
    assert "whitespace" in hint
    assert "overflow-hidden" in hint


def test_layout_fix_hint_from_cli_output_flags_empty_chart_container():
    cli_text = (
        "[page-3.pptx.html][whitespace] 语义跨度 6.4%, 空白 308px - "
        "section → div#chart-1\n"
        "[page-3.pptx.html][v-gap] 内容下方空隙 290px - div.min-h-0.border chart"
    )
    hint = _layout_fix_hint_from_cli_output(cli_text)
    assert "option" in hint
    assert "flex" in hint


def test_post_check_layout_hints_flag_null_chart_option():
    html = (
        '<div id="chart-1"></div>\n'
        "<script>\n"
        "  /* 把下方 const option = null 替换为对象 */\n"
        "  const option = null;\n"
        "  if (!option) return;\n"
        "</script>\n"
    )
    hints = _post_check_layout_hints(html)
    assert any("option 未填" in h for h in hints)


def test_build_structural_page_directive_agenda_forbids_section():
    directive = cp._build_structural_page_directive(
        {"structural_page_request": "agenda", "page_count": 4}
    )
    assert "agenda" in directive
    assert "禁止生成 section" in directive
    assert "结构页 1" not in directive
    assert "内容组 1" not in directive


def test_build_agenda_mapping_directive_maps_dimensions_to_content_pages():
    mapping = cp._build_agenda_mapping_directive(
        {
            "structural_page_request": "agenda",
            "user_dimensions": ["历史背景", "事件经过"],
            "user_structure": "封面、目录、历史背景、事件经过",
        }
    )
    assert "agenda 模式" in mapping
    assert "历史背景" in mapping
    assert "禁止生成 section" in mapping


def test_build_layout_patch_prompt_only_allows_main_changes():
    seed = (
        "<html><head><title>t</title></head><body>"
        '<main class="page-main">'
        "<!-- CHART_SCAFFOLD_BEGIN\n"
        "<script>const option = null;</script>\n"
        "CHART_SCAFFOLD_END -->"
        "</main></body></html>"
    )
    current = (
        "<html><head><title>t</title></head><body>"
        '<main class="page-main">'
        "<script>const option = { series: [{ type: 'bar', data: [1] }] };</script>"
        "</main></body></html>"
    )
    prompt = _build_layout_patch_prompt(
        page_number=3,
        style_id="business-classic",
        current_html=current,
        seed_html=seed,
        fix_hint="overflow：减内容",
        outline_page="页面类型: content",
    )
    assert "唯一底稿" in prompt
    assert "只允许修改" in prompt
    assert "overflow" in prompt
    assert "待修补" in prompt
    # 当前稿在前，且含已填 option
    assert prompt.index("当前 HTML") < prompt.index("Page Chrome 对照")
    assert "series: [{ type: 'bar'" in prompt
    # 不再塞完整 seed 空骨架
    assert "const option = null;" not in prompt
    assert "CHART_SCAFFOLD_BEGIN" not in prompt
    assert "PAGE_CONTENT 已省略" in prompt
    assert "原样保留" in prompt
    assert "禁止重写" in prompt
    assert "本页研究素材" not in prompt


def test_build_layout_patch_prompt_requires_fill_null_option():
    seed = (
        "<html><head><title>t</title></head><body>"
        '<main class="page-main"><div>chrome</div></main></body></html>'
    )
    current = (
        "<html><head><title>t</title></head><body>"
        '<main class="page-main">'
        '<div id="chart-1"></div>'
        "<script>\n"
        "  const option = null;\n"
        "  if (!option) return;\n"
        "</script>"
        "</main></body></html>"
    )
    prompt = _build_layout_patch_prompt(
        page_number=3,
        style_id="business-classic",
        current_html=current,
        seed_html=seed,
        fix_hint="whitespace：图表区空白",
        outline_page="页面类型: data",
        research_page="| 指标 | Hopper | Blackwell |\n| Token | 54K | 2.8M |",
    )
    assert "option = null" in prompt
    assert "本页研究素材" in prompt
    assert "2.8M" in prompt
    assert "禁止假装调间距" in prompt
    assert "换成真实 option 对象" in prompt
    assert "禁止重写 `<script>` 内 ECharts 配置" not in prompt


def test_layout_patch_seed_chrome_omits_main_and_scaffold():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
        _layout_patch_seed_chrome_reference,
    )

    seed = (
        "<html><head><title>seed</title></head><body>"
        '<header class="page-header"><h1>T</h1></header>'
        '<main class="flex-1">'
        "<!-- CHART_SCAFFOLD_BEGIN\n"
        "<script>const option = null;</script>\n"
        "CHART_SCAFFOLD_END -->"
        "</main>"
        '<div class="page-footer"><p>f</p></div>'
        "</body></html>"
    )
    ref = _layout_patch_seed_chrome_reference(seed)
    assert "PAGE_CONTENT 已省略" in ref
    assert "CHART_SCAFFOLD_BEGIN" not in ref
    assert "const option = null" not in ref
    assert "<title>seed</title>" in ref
    assert 'main class="flex-1"' in ref


@pytest.mark.asyncio
async def test_reconcile_missing_pages_keeps_layout_warning_pages(tmp_path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    html = (
        '<!DOCTYPE html><html><body>'
        '<div class="ppt-slide h-[720px]">'
        '<main class="flex-1"><section>content</section></main>'
        "</div></body></html>"
    )
    for i in range(1, 3):
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
        total_pages=2,
        reported_missing=[],
        reported_page_files=["page-1.pptx.html", "page-2.pptx.html"],
    )
    assert missing == []
    assert files == ["page-1.pptx.html", "page-2.pptx.html"]
