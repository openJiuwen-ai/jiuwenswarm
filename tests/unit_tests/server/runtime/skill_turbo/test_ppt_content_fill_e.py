# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Batch E: content-fill density slim + custom content-template path."""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    _CONTENT_FILL_DENSITY_CHECKLIST,
    _build_content_template_fill_prompt,
    _count_filled_chart_options,
    _count_null_chart_options,
    _extract_designer_section,
    _fix_chart_scaffold_activation,
    _layout_patch_regressed_chart_options,
    _layout_patch_still_unfilled_chart_options,
    _uses_content_template_fill,
    _validate_custom_content_template_fill_output,
)

# _is_valid_html requires len >= 200
_MINIMAL_SEED_HTML = """<!DOCTYPE html>
<html><head><title>{{PAGE_TITLE}}</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style>@layer utilities{.content-safe{width:1220px}}</style>
</head>
<body>
<div class="ppt-slide w-[1280px] h-[720px]">
  <div class="content-safe">
    <header class="flex-shrink-0 page-header"><h1 class="page-title">{{PAGE_TITLE}}</h1></header>
    <main class="flex-1 min-h-0 page-main">{{PAGE_CONTENT}}</main>
    <div class="flex-shrink-0 page-footer"><p class="page-footer-note">{{PAGE_FOOTER}}</p></div>
  </div>
</div>
</body></html>
"""

_OUTLINE_CONTENT = """### P3: 市场趋势
- **类型**: content
- **研究需求**: ✅ 需要调研
- **标题**: 市场趋势
- **内容概要**: 概要足够长用于校验
"""


def test_extract_designer_for_fill_uses_density_checklist_only():
    long_designer = (
        "## 用户显式要求优先\n必须遵守用户色板\n\n"
        "**E. 写前版面意图**\n长文版面意图若干行\n\n"
        "## 关键原则\n禁止空卡片\n"
    )
    out = _extract_designer_section(
        long_designer,
        for_content_template_fill=True,
        appendix_text="## 弹性布局模式\n很多附录文字\n",
    )
    assert _CONTENT_FILL_DENSITY_CHECKLIST in out
    assert "写前版面意图" not in out
    assert "弹性布局模式" not in out


def test_extract_designer_for_fill_appends_charts_when_requested():
    charts = "## 图表与数据可视化\n优先激活 CHART_SCAFFOLD\n"
    out = _extract_designer_section(
        "",
        for_content_template_fill=True,
        include_charts=True,
        charts_text=charts,
    )
    assert _CONTENT_FILL_DENSITY_CHECKLIST in out
    assert "CHART_SCAFFOLD" in out


def test_fix_chart_scaffold_activation_strips_html_comment_markers():
    html = (
        '<div id="chart-1"></div>\n'
        "<!-- CHART_SCAFFOLD_BEGIN\n"
        "<script>\n"
        "  /* CHART_SCAFFOLD_BEGIN stays in JS block comment */\n"
        '  const el = document.getElementById("chart-1");\n'
        "  const option = { series: [{ type: 'bar', data: [1, 2] }] };\n"
        "  echarts.init(el).setOption(option);\n"
        "</script>\n"
        "CHART_SCAFFOLD_END -->\n"
    )
    fixed = _fix_chart_scaffold_activation(html)
    assert "<!-- CHART_SCAFFOLD_BEGIN" not in fixed
    assert "CHART_SCAFFOLD_END -->" not in fixed
    assert "<script>" in fixed
    assert "CHART_SCAFFOLD_BEGIN stays in JS block comment" in fixed
    assert fixed == _fix_chart_scaffold_activation(fixed)


def test_fix_chart_scaffold_activation_noop_without_markers():
    html = "<div id='chart-1'></div><script>echarts.init(el)</script>"
    assert _fix_chart_scaffold_activation(html) is html


def test_count_filled_chart_options_ignores_comment_and_null():
    filled = (
        "<script>\n"
        "  /* 把下方 const option = null 替换为对象 */\n"
        "  const option = { series: [{ type: 'bar', data: [1] }] };\n"
        "</script>\n"
    )
    empty = (
        "<script>\n"
        "  /* 把下方 const option = null 替换为对象 */\n"
        "  const option = null;\n"
        "  if (!option) return;\n"
        "</script>\n"
    )
    assert _count_filled_chart_options(filled) == 1
    assert _count_filled_chart_options(empty) == 0
    assert _count_null_chart_options(empty) == 1
    assert _count_null_chart_options(filled) == 0


def test_layout_patch_regressed_chart_options_detects_null_rollback():
    before = (
        '<div id="chart-1"></div>\n'
        "<script>\n"
        "  const option = { series: [{ type: 'bar', data: [1, 2] }] };\n"
        "  echarts.init(el).setOption(option);\n"
        "</script>\n"
    )
    after = (
        '<div id="chart-1"></div>\n'
        "<script>\n"
        "  const option = null;\n"
        "  if (!option) return;\n"
        "  echarts.init(el).setOption(option);\n"
        "</script>\n"
    )
    assert _layout_patch_regressed_chart_options(before, after)
    assert not _layout_patch_regressed_chart_options(before, before)
    assert not _layout_patch_regressed_chart_options(after, after)


def test_layout_patch_still_unfilled_chart_options():
    empty = (
        "<script>\n"
        "  const option = null;\n"
        "  if (!option) return;\n"
        "</script>\n"
    )
    filled = (
        "<script>\n"
        "  const option = { series: [{ type: 'bar', data: [1] }] };\n"
        "</script>\n"
    )
    assert _layout_patch_still_unfilled_chart_options(empty, empty)
    assert not _layout_patch_still_unfilled_chart_options(empty, filled)
    assert not _layout_patch_still_unfilled_chart_options(filled, filled)


def test_content_fill_prompt_omits_full_outline():
    prompt = _build_content_template_fill_prompt(
        page_number=3,
        style_id="business-classic",
        style_text="style body",
        outline_page=_OUTLINE_CONTENT,
        research_page="research notes",
        outline_full="### P1: 封面\n### P2: 目录\n### P99: 不应出现的全文页",
        seed_html=_MINIMAL_SEED_HTML,
    )
    assert "大纲 — 本页规划" in prompt
    assert "市场趋势" in prompt
    assert "P99" not in prompt
    assert "不应出现的全文页" not in prompt
    assert "完整 outline" not in prompt.lower()


def test_uses_content_template_fill_includes_custom():
    assert _uses_content_template_fill("custom", "content", _OUTLINE_CONTENT)
    assert not _uses_content_template_fill("custom", "cover", _OUTLINE_CONTENT)


def test_validate_custom_rejects_single_root_block():
    filled = (
        _MINIMAL_SEED_HTML.replace("{{PAGE_TITLE}}", "真实标题足够长")
        .replace(
            "{{PAGE_CONTENT}}",
            '<div class="w-full flex-1 min-h-0"><p>唯一根容器包住全部正文内容</p></div>',
        )
        .replace("{{PAGE_FOOTER}}", "来源：测试报告")
    )
    ok, reason = _validate_custom_content_template_fill_output(_MINIMAL_SEED_HTML, filled)
    assert not ok
    assert reason == "custom_page_content_blocks"


def test_validate_custom_accepts_two_direct_children():
    filled = (
        _MINIMAL_SEED_HTML.replace("{{PAGE_TITLE}}", "真实标题足够长")
        .replace(
            "{{PAGE_CONTENT}}",
            (
                '<div class="flex-shrink-0"><p>结论条要点说明文字</p></div>'
                '<div class="flex-1 min-h-0"><p>主体分栏正文内容</p></div>'
            ),
        )
        .replace("{{PAGE_FOOTER}}", "来源：测试报告")
    )
    ok, reason = _validate_custom_content_template_fill_output(_MINIMAL_SEED_HTML, filled)
    assert ok, reason


def test_custom_fill_prompt_branch_rules():
    prompt = _build_content_template_fill_prompt(
        page_number=3,
        style_id="custom",
        style_text="custom style",
        outline_page=_OUTLINE_CONTENT,
        research_page="research",
        outline_full="FULL_OUTLINE_SHOULD_NOT_APPEAR",
        seed_html=_MINIMAL_SEED_HTML,
    )
    assert "自定义风格" in prompt
    assert "至少两个直接子块" in prompt
    assert "THEME_CSS_*" in prompt or "THEME" in prompt
    assert "FULL_OUTLINE_SHOULD_NOT_APPEAR" not in prompt
